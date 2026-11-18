"""Retiring an HQ room: the task script against a fake Synapse, and the local target check."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import pytest

from claude_kitchen import hq_room, hq_room_task

ROOMS = {"!general": None, "!feedback": None, "!space": "m.space", "!team-space": "m.space", "!guests": "m.space", "!work": None}
ALIASES = {"#general:plow.co": "!general", "#plow-feedback:plow.co": "!feedback", "#rooms:plow.co": "!space", "#guests:plow.co": "!guests",
           "#work:plow.co": "!work"}


@pytest.fixture
def synapse(monkeypatch):
    """A fake Synapse: records every request, serves the reads the task makes."""
    seen, aliases, children = [], dict(ALIASES), {"!work": {"via": ["plow.co"]}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def handle_any(self):
            path = unquote(self.path)
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"null")
            seen.append((self.command, path, body))
            route = {
                ("POST", "/_matrix/client/v3/login"): lambda: (200, {"access_token": "tok"}),
                ("POST", "/_matrix/client/v3/logout"): lambda: (200, {}),
            }.get((self.command, path))
            if route:
                return self.reply(*route())
            if path.startswith("/_matrix/client/v3/directory/room/"):
                alias = path.rsplit("/", 1)[1]
                if self.command == "DELETE":
                    aliases.pop(alias)
                    return self.reply(200, {})
                return self.reply(200, {"room_id": aliases[alias]}) if alias in aliases else self.reply(404, {})
            if path == "/_matrix/client/v3/rooms/!general/state/swarm.common_rooms/":
                return self.reply(200, {"rooms": ["#general:plow.co", "#plow-feedback:plow.co"]})
            if path.startswith("/_synapse/admin/v1/rooms/"):
                room = path.rsplit("/", 1)[1]
                return self.reply(200, {"name": room, "room_type": ROOMS[room]}) if room in ROOMS else self.reply(404, {})
            if self.command == "DELETE" and path.startswith("/_synapse/admin/v2/rooms/"):
                return self.reply(200, {"delete_id": "d1"})
            if path == "/_synapse/admin/v2/rooms/delete_status/d1":
                return self.reply(200, {"status": "complete", "shutdown_room": {"kicked_users": ["@a:plow.co"]}})
            if path.endswith("/aliases"):
                room = path.split("/")[-2]
                return self.reply(200, {"aliases": [a for a, r in aliases.items() if r == room]})
            if "/state/m.space.child/" in path:
                room = path.rsplit("/", 1)[1]
                if self.command == "PUT":
                    children[room] = body
                    return self.reply(200, {})
                return self.reply(200, children[room]) if room in children else self.reply(404, {})
            self.reply(404, {})

        do_GET = do_POST = do_PUT = do_DELETE = handle_any

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(hq_room_task, "BASE", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("SWARM_PLOW_PASSWORD", "pw")
    yield seen
    server.shutdown()


def run(target, capsys):
    hq_room_task.main(target)
    out = capsys.readouterr().out
    assert "tok" not in out and "pw" not in out
    return hq_room.parse(out)


def test_a_room_is_retired_with_purge_off_and_taken_out_of_the_directory(synapse, capsys):
    result = run("#work:plow.co", capsys)
    deletes = [b for m, p, b in synapse if m == "DELETE" and p == "/_synapse/admin/v2/rooms/!work"]
    assert deletes == [{"purge": False, "block": False, "message": "This room was closed by Plow."}]
    assert result["status"] == "complete"
    assert result["after"]["aliases"] == [] and result["after"]["in_spaces"] == []
    assert synapse[-1][:2] == ("POST", "/_matrix/client/v3/logout")


@pytest.mark.parametrize("target, refusal", [
    ("#general:plow.co", "#general is one of HQ's core rooms"),
    ("!feedback", "a common room"),
    ("!team-space", "a Space"),
    ("!elsewhere", "no room on HQ"),
    ("#gone:plow.co", "no room on HQ"),
])
def test_a_core_or_unknown_room_is_refused_before_any_change(synapse, capsys, target, refusal):
    assert refusal in run(target, capsys)["refused"]
    assert not [m for m, p, _ in synapse if m in ("DELETE", "PUT")]
    assert synapse[-1][:2] == ("POST", "/_matrix/client/v3/logout")


@pytest.mark.parametrize("target", ["#work:plow.swarm.plow.co", "!abc:odio.family", "work", "#a:plow.co.evil", "!a b"])
def test_a_target_off_hq_never_reaches_aws(monkeypatch, target):
    monkeypatch.setattr(hq_room, "run_in_task", lambda _: pytest.fail("reached AWS"))
    with pytest.raises(SystemExit, match="not an HQ room"):
        hq_room.retire(target, "Sam")
