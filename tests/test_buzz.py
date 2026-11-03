import asyncio
import itertools
import json
import time

import nostr_sdk
import pytest

from claude_kitchen.buzz import Bridge

KEYS = nostr_sdk.Keys.generate()  # the sous's own member key
CEDAR, SAM, ASPEN = KEYS.public_key().to_hex(), "5" * 64, "7" * 64
IDS = itertools.count()
DM = "62db2030-9ac5-472b-8533-dadf656179c9"
SUB = "messages-1"  # a fresh bridge's first message subscription
EOSE = ["EOSE", SUB]  # the relay's end of its stored events


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def bridge(tmp_path):
    delivered = []

    async def deliver(content, meta):
        delivered.append((content, meta))

    b = Bridge(KEYS, deliver, tmp_path / "last_seen.json")
    b.ws = FakeSocket()
    return b, delivered


def event(author, kind, content="", tags=()):
    """A relay-delivered event; the bridge trusts the relay, so it is unsigned."""
    return {"id": f"{next(IDS):064x}", "pubkey": author, "created_at": int(time.time()), "kind": kind,
            "tags": list(tags), "content": content}


def membership(*members):
    return ["EVENT", "members", event(SAM, 39002, tags=[["d", DM], *(["p", m] for m in members)])]


def profile(author, name):
    return ["EVENT", "profiles", event(author, 0, json.dumps({"display_name": name}))]


def say(author, text, sub=SUB):
    return ["EVENT", sub, event(author, 9, text, [["h", DM]])]


@pytest.mark.parametrize("frames,expected", [
    pytest.param([profile(SAM, "Sam"), say(SAM, "hi Cedar")], [("hi Cedar", "Sam")], id="named"),
    pytest.param([say(ASPEN, "hey")], [("hey", ASPEN[:8])], id="no-profile"),
    pytest.param([say(CEDAR, "my own reply"), say(SAM, " \n")], [], id="own-and-blank"),
])
def test_delivers_other_members_texts_by_name(tmp_path, frames, expected):
    b, delivered = bridge(tmp_path)

    async def go():
        for f in [membership(SAM, CEDAR, ASPEN), EOSE, *frames]:
            await b.handle(f)

    asyncio.run(go())
    assert delivered == [(text, {"from": name, "chat": DM}) for text, name in expected]


def test_a_text_is_delivered_once_across_replays_and_restarts(tmp_path):
    text = say(SAM, "once")
    first, delivered = bridge(tmp_path)
    asyncio.run(first.handle(membership(SAM, CEDAR)))
    asyncio.run(first.handle(EOSE))
    asyncio.run(first.handle(text))
    asyncio.run(first.handle(text))
    again, redelivered = bridge(tmp_path)
    asyncio.run(again.handle(membership(SAM, CEDAR)))
    asyncio.run(again.handle(EOSE))
    asyncio.run(again.handle(text))
    assert len(delivered) == 1 and redelivered == []
    resubscribe = [f for f in again.ws.sent if f[:2] == ["REQ", SUB]][-1]
    assert resubscribe[2] == {"kinds": [9], "#h": [DM], "since": text[2]["created_at"]}


def test_follows_a_channel_once_added_and_replies_only_where_it_is_a_member(tmp_path):
    b, _ = bridge(tmp_path)

    async def go():
        with pytest.raises(ValueError, match="not a member"):
            await b.reply(DM, "too early")
        await b.handle(membership(SAM, CEDAR))
        task = asyncio.create_task(b.reply(DM, "hello Sam"))
        await asyncio.sleep(0)
        sent = b.ws.sent[-1][1]
        await b.handle(["OK", sent["id"], True, ""])
        return sent, await task

    sent, result = asyncio.run(go())
    assert any(f[:2] == ["REQ", SUB] and f[2]["#h"] == [DM] for f in b.ws.sent)
    assert (sent["kind"], sent["content"], sent["tags"]) == (9, "hello Sam", [["h", DM]])
    assert result == f"sent {sent['id']}"


def test_an_interrupted_replay_is_resumed_from_before_it_started(tmp_path):
    older, newer = say(SAM, "older"), say(SAM, "newer")
    older[2]["created_at"] += 10
    newer[2]["created_at"] += 20
    first, delivered = bridge(tmp_path)
    asyncio.run(first.handle(membership(SAM, CEDAR)))
    since = first.state["since"]
    asyncio.run(first.handle(newer))  # stored events arrive newest first; socket drops
    again, redelivered = bridge(tmp_path)
    asyncio.run(again.handle(membership(SAM, CEDAR)))
    asyncio.run(again.handle(newer))
    asyncio.run(again.handle(older))
    asyncio.run(again.handle(EOSE))
    assert delivered == [] and again.ws.sent[-1][2]["since"] == since
    assert [text for text, _ in redelivered] == ["older", "newer"]


def test_a_replaced_subscription_neither_delivers_nor_ends_the_new_replay(tmp_path):
    b, delivered = bridge(tmp_path)
    older, newer = say(SAM, "older", "messages-2"), say(SAM, "newer", "messages-2")
    older[2]["created_at"] += 10
    newer[2]["created_at"] += 20

    async def go():
        await b.handle(membership(SAM, CEDAR))
        await b.handle(membership(SAM, CEDAR, ASPEN))  # a new member re-issues the subscription
        await b.handle(say(SAM, "stale"))
        await b.handle(EOSE)
        await b.handle(newer)
        await b.handle(older)
        await b.handle(["EOSE", "messages-2"])

    asyncio.run(go())
    assert ["CLOSE", SUB] in b.ws.sent
    assert [text for text, _ in delivered] == ["older", "newer"]
