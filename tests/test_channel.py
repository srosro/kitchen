"""Tests for the kitchen channel MCP server."""
import asyncio
import json
import os
import socket as sock_mod
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from claude_kitchen.channel import _claim_socket, deliver_to_socket, handle_connection, send_to_socket


class TestHandleConnection:
    """Test the socket connection handler."""

    def test_parses_json_line(self):
        data = {"cook": "eng", "summary": "tests pass", "ts": "2026-03-26T10:00:00Z"}

        async def run():
            reader = asyncio.StreamReader()
            reader.feed_data(json.dumps(data).encode() + b"\n")
            reader.feed_eof()
            notified = []
            await handle_connection(reader, None, lambda d: _append(notified, d))
            return notified

        async def _append(lst, d):
            lst.append(d)

        notified = asyncio.run(run())
        assert notified == [data]

    def test_ignores_invalid_json(self):
        async def run():
            reader = asyncio.StreamReader()
            reader.feed_data(b"not json\n")
            reader.feed_eof()
            notified = []
            await handle_connection(reader, None, lambda d: _append(notified, d))
            return notified

        async def _append(lst, d):
            lst.append(d)

        notified = asyncio.run(run())
        assert notified == []

    def test_ignores_empty_connection(self):
        async def run():
            reader = asyncio.StreamReader()
            reader.feed_eof()
            notified = []
            await handle_connection(reader, None, lambda d: _append(notified, d))
            return notified

        async def _append(lst, d):
            lst.append(d)

        notified = asyncio.run(run())
        assert notified == []


class TestChannelServer:
    """The real channel server, driven over its MCP stdio and its real socket."""

    def test_message_past_asyncios_default_limit_reaches_the_sous(self):
        summary = "x" * 70_000  # asyncio's default line limit is 64 KiB
        with tempfile.TemporaryDirectory(dir="/tmp") as home:
            sock_path = Path(home) / ".claude-kitchen" / "k" / "kitchen.sock"
            server = subprocess.Popen(
                [sys.executable, "-c", "from claude_kitchen.channel import main; main('k')"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                env={**os.environ, "HOME": home},
            )

            def send(message):
                server.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
                server.stdin.flush()

            try:
                send({"id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                }})
                assert json.loads(server.stdout.readline())["id"] == 1
                send({"method": "notifications/initialized"})

                deliver_to_socket(sock_path, {"cook": "eng", "summary": summary, "ts": "now"})
                # Comes through even if the long message is dropped, so a
                # regression fails the assert below instead of hanging the read.
                deliver_to_socket(sock_path, {"cook": "eng", "summary": "after", "ts": "now"})

                pushed = json.loads(server.stdout.readline())
            finally:
                server.kill()
                server.wait()

        assert pushed["method"] == "notifications/claude/channel"
        assert len(pushed["params"]["content"]) == len(summary)


class TestSendToSocket:
    """Test the hook-side socket client."""

    def test_sends_json_to_socket(self):
        # Use /tmp to avoid AF_UNIX path length limits
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            sock_path = Path(td) / "k.sock"
            received = []

            def run_server():
                srv = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
                srv.bind(str(sock_path))
                srv.listen(1)
                conn, _ = srv.accept()
                data = conn.recv(4096)
                received.append(json.loads(data))
                conn.close()
                srv.close()

            t = threading.Thread(target=run_server)
            t.start()
            time.sleep(0.05)

            send_to_socket(sock_path, {"cook": "eng", "summary": "done"})
            t.join(timeout=2)

            assert len(received) == 1
            assert received[0]["cook"] == "eng"

    def test_dead_socket_is_loud_but_not_fatal(self, tmp_path, capsys):
        # A send to a non-listening socket must NOT raise (the cook hook keeps
        # running) but MUST surface a visible error naming the path + errno.
        dead = tmp_path / "nonexistent.sock"
        send_to_socket(dead, {"cook": "eng", "summary": "done"})  # does not raise
        err = capsys.readouterr().err
        assert "failed to notify sous" in err
        assert str(dead) in err
        assert "errno" in err


class TestClaimSocket:
    """One server per kitchen owns its socket path, through a lock held for life."""

    def test_a_second_server_stands_down_while_the_first_holds_the_path(self, tmp_path):
        # Both used to pass before either bound, and asyncio's bind removes an
        # existing socket file, so the later one orphaned the earlier's listener.
        sock_path = tmp_path / "k.sock"
        owner = _claim_socket(sock_path)
        with pytest.raises(SystemExit) as exc:
            _claim_socket(sock_path)
        assert exc.value.code == 0
        (tmp_path / "k.sock.lock").unlink()  # `kitchen close` hands the path on while it still runs
        _claim_socket(sock_path).close()
        owner.close()
        _claim_socket(sock_path).close()  # released: the next server claims it

    def test_a_live_listener_without_the_lock_is_left_alone(self):
        # A server from before the lock still owns its socket during an upgrade.
        # /tmp, not tmp_path: macOS's pytest paths outrun AF_UNIX's length limit.
        with tempfile.TemporaryDirectory(dir="/tmp") as td, \
                sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM) as live:
            sock_path = Path(td) / "k.sock"
            live.bind(str(sock_path))
            live.listen(1)
            with pytest.raises(SystemExit) as exc:
                _claim_socket(sock_path)
            assert exc.value.code == 0
            assert sock_path.is_socket()

    def test_a_stale_socket_is_cleared_for_binding(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            sock_path = Path(td) / "k.sock"
            dead = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
            dead.bind(str(sock_path))
            dead.close()
            _claim_socket(sock_path).close()
            assert not sock_path.exists()

    def test_a_path_that_is_not_a_socket_is_left_alone_loudly(self, tmp_path, capsys):
        sock_path = tmp_path / "k.sock"
        sock_path.write_text("not a socket")
        with pytest.raises(SystemExit) as exc:
            _claim_socket(sock_path)
        assert exc.value.code == 1
        assert sock_path.read_text() == "not a socket"
        assert "refusing to bind" in capsys.readouterr().err


class TestChannelCapabilityAdvertisement:
    """The regression that made every kitchen one restart from deaf: mcp 2.0.0 can
    serve the 2026-07-28 era, where Claude never receives ServerCapabilities at
    all, so `claude/channel` is never seen and every cook report is dropped —
    silently, with a clean connection and a bound socket.

    These run against the REAL SDK, not a mock, so the next SDK move fails in CI
    instead of in a kitchen."""

    def _capabilities(self):
        """Built exactly the way run_server builds them, against the REAL SDK."""
        from mcp.server.lowlevel import NotificationOptions, Server
        from claude_kitchen.channel import CHANNEL_CAPABILITY
        server = Server("kitchen")
        return server.create_initialization_options(
            notification_options=NotificationOptions(),
            experimental_capabilities={CHANNEL_CAPABILITY: {}},
        ).capabilities

    def test_advertised_in_every_place_this_sdk_carries_capabilities(self):
        from claude_kitchen.channel import CHANNEL_CAPABILITY, _capability_maps
        caps = self._capabilities()
        maps = _capability_maps(caps)
        assert maps, "the SDK exposes no free-form capability map — the shape changed"
        for name in maps:
            assert CHANNEL_CAPABILITY in (getattr(caps, name) or {}), (
                f"claude/channel is missing from ServerCapabilities.{name}; a sous "
                f"negotiating the era that reads {name} would be silently deaf"
            )

    def test_a_new_capability_map_is_a_failure_not_a_shrug(self):
        """mcp 2.0.0 added `extensions` alongside `experimental`, and the server
        kept advertising only `experimental`. Nothing failed — Claude just went
        quiet. Any SDK that adds another capability map must fail here."""
        caps = self._capabilities()
        assert _CAP_MAPS(caps) == ["experimental"], (
            f"this SDK carries capabilities in {_CAP_MAPS(caps)}; channel.py "
            f"advertises claude/channel only in `experimental`"
        )

    def test_a_server_that_cannot_deliver_refuses_to_start(self):
        from claude_kitchen.channel import _refuse_if_deaf
        caps = self._capabilities()
        caps.experimental = {}        # the exact shape of the outage
        with pytest.raises(SystemExit) as e:
            _refuse_if_deaf(caps)
        assert "experimental" in str(e.value)

    def test_refuses_before_it_claims_the_socket(self, tmp_path, monkeypatch):
        """A deaf server must not own kitchen.sock — that is what stopped the
        working replacement from binding during the outage."""
        import claude_kitchen.channel as ch
        claimed = []
        monkeypatch.setattr(ch, "_claim_socket", lambda p: claimed.append(p))
        monkeypatch.setattr(ch, "state_dir", lambda k: tmp_path, raising=False)
        monkeypatch.setattr("claude_kitchen.state.state_dir", lambda k: tmp_path)
        monkeypatch.setattr(ch, "_refuse_if_deaf", _boom)
        with pytest.raises(SystemExit):
            asyncio.run(ch.run_server("whatever"))
        assert claimed == [], "the socket was claimed by a server that cannot deliver"
        assert not (tmp_path / "kitchen.sock").exists()


def _CAP_MAPS(caps):
    from claude_kitchen.channel import _capability_maps
    return _capability_maps(caps)


def _boom(_caps):
    raise SystemExit("deaf")
