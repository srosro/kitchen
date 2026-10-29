"""Tests for the plow-chat channel server, against a fake Plow API."""
import asyncio
import json
import tempfile
from pathlib import Path

import httpx
import pytest

from claude_kitchen.plow_chat import Bridge, Config, load_config
from tests.fake_plow import TOKEN, FakePlow

SAM, PLUCAS, STRANGER = "+15550000001", "+15550000002", "+15550000099"
HOME, OTHER, GONE = "cht_home", "cht_other", "cht_gone"


async def eventually(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def run_bridge(scenario, fake=None, cursor=None, chats=(HOME,)):
    """Run `scenario(fake, bridge, delivered)` with the bridge's socket
    supervisor live against `fake` (a fresh one by default). Pass the same
    fake and cursor twice to restart the server against the same chat."""
    fake = fake or FakePlow()
    cursor = cursor or Path(tempfile.mkdtemp()) / "last_seen.json"

    async def main():
        base_url, server = await fake.serve()
        config = Config(token=TOKEN, senders={SAM: "sam", PLUCAS: "plucas"},
                        chats=frozenset(chats), base_url=base_url)
        delivered = []

        async def deliver(content, meta):
            delivered.append((content, meta))

        async with httpx.AsyncClient(base_url=base_url, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
            bridge = Bridge(config, client, deliver, cursor)
            supervisor = asyncio.create_task(bridge.run())
            await eventually(lambda: fake.sockets)
            try:
                await scenario(fake, bridge, delivered)
            finally:
                supervisor.cancel()
                server.should_exit = True
    asyncio.run(main())


def test_allowed_sender_is_delivered():
    async def scenario(fake, bridge, delivered):
        await fake.inbound(HOME, PLUCAS, "can you look at the mba?")
        await eventually(lambda: delivered)
        assert delivered == [("can you look at the mba?", {"from": "plucas", "chat": HOME})]
    run_bridge(scenario)


@pytest.mark.parametrize("chat, handle", [(HOME, STRANGER), (OTHER, SAM)], ids=["sender", "chat"])
def test_non_allowlisted_message_is_dropped_and_logged(chat, handle, capsys):
    async def scenario(fake, bridge, delivered):
        dropped = await fake.inbound(chat, handle, "let me in")
        await fake.inbound(HOME, SAM, "marker")  # once this lands, the drop was processed
        await eventually(lambda: delivered)
        assert delivered == [("marker", {"from": "sam", "chat": HOME})]
        assert f"dropped {dropped['uid']}" in capsys.readouterr().err
    run_bridge(scenario)


def test_reply_refuses_chat_not_on_allowlist_and_sends_to_allowed():
    async def scenario(fake, bridge, delivered):
        with pytest.raises(ValueError, match="not on the plow-chat allowlist"):
            await bridge.reply(OTHER, "hello")
        await bridge.reply(HOME, "on it")
        assert fake.sent == [(HOME, "on it")]
        await fake.inbound(HOME, SAM, "marker")
        await eventually(lambda: delivered)
        assert [c for c, _ in delivered] == ["marker"], "the reply's outbound echo must not reach the sous"
    run_bridge(scenario)


def test_reconnect_catches_up_without_replay_or_duplicates():
    async def scenario(fake, bridge, delivered):
        await fake.inbound(HOME, SAM, "before")  # live, delivered once
        await eventually(lambda: delivered)
        await fake.drop_sockets()
        fake.add(HOME, SAM, "during the gap")
        await eventually(lambda: fake.sockets)  # reconnected with a fresh ticket
        await fake.inbound(HOME, SAM, "after")
        await eventually(lambda: len(delivered) == 3)
        await asyncio.sleep(0.2)
        assert [c for c, _ in delivered] == ["before", "during the gap", "after"]

    fake = FakePlow()
    fake.add(HOME, SAM, "history from last week")  # a first-ever start must not replay it
    run_bridge(scenario, fake)


def test_restart_delivers_texts_sent_while_down_exactly_once(tmp_path):
    fake, cursor = FakePlow(), tmp_path / "last_seen.json"

    async def before_restart(fake, bridge, delivered):
        await fake.inbound(HOME, SAM, "before restart")
        await eventually(lambda: delivered)

    async def after_restart(fake, bridge, delivered):
        await eventually(lambda: delivered)
        await asyncio.sleep(0.2)
        assert [c for c, _ in delivered] == ["while down"]

    run_bridge(before_restart, fake, cursor)
    fake.add(HOME, SAM, "while down")
    run_bridge(after_restart, fake, cursor)
    assert cursor.stat().st_mode & 0o777 == 0o600



def test_a_chat_plow_refuses_is_skipped_and_logged(capsys):
    fake = FakePlow()
    fake.missing.add(GONE)

    async def scenario(fake, bridge, delivered):
        await fake.inbound(HOME, SAM, "still reachable")
        await eventually(lambda: delivered)
        assert f"HTTP 404 reading chat {GONE}; skipped" in capsys.readouterr().err

    run_bridge(scenario, fake, chats=(HOME, GONE))


def test_a_flood_of_dropped_texts_never_replays_old_ones():
    async def scenario(fake, bridge, delivered):
        await fake.inbound(HOME, SAM, "real")
        await eventually(lambda: delivered)
        for i in range(2001):
            await fake.inbound(OTHER, STRANGER, f"spam {i}")
        await fake.drop_sockets()
        await eventually(lambda: fake.sockets)
        await fake.inbound(HOME, SAM, "marker")
        await eventually(lambda: len(delivered) >= 2)
        await asyncio.sleep(0.2)
        assert [c for c, _ in delivered] == ["real", "marker"]

    run_bridge(scenario)


def test_texts_stamped_at_the_same_instant_are_each_delivered():
    async def scenario(fake, bridge, delivered):
        first = await fake.inbound(HOME, SAM, "first")
        await eventually(lambda: delivered)
        await fake.inbound(HOME, PLUCAS, "same instant", created_at=first["created_at"])
        await eventually(lambda: len(delivered) == 2)
        await asyncio.sleep(0.2)
        assert [c for c, _ in delivered] == ["first", "same instant"]

    run_bridge(scenario)

def test_load_config_refuses_token_readable_by_others(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"senders": {SAM: "sam"}, "chats": [HOME]}))
    token = tmp_path / "token"
    token.write_text(TOKEN)
    token.chmod(0o644)
    with pytest.raises(SystemExit, match="chmod 600"):
        load_config(tmp_path)
    token.chmod(0o600)
    assert load_config(tmp_path).token == TOKEN
