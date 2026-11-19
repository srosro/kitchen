"""Tests for the plow-chat channel server, against a fake Plow API."""
import argparse
import asyncio
import io
import json
import re
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import unquote

import httpx
import pytest

from claude_kitchen import plow_chat
from claude_kitchen.channel import reply_server
from claude_kitchen.cli import cmd_hook
from claude_kitchen.plow_chat import CREDENTIALS, Bridge, call, content_type_of, read_credentials
from tests.fake_plow import SIGNATURE, TOKEN, FakePlow

OWNER, TEAMMATE, STRANGER = "+15550000001", "+15550000002", "+15550000099"
SPRUCE = {"type": "agent", "relationship": "peer", "line": {"display_name": "Spruce", "uid": "ln_peer"}}
PLOW = {"type": "system", "name": "Plow"}
NAMELESS = {"type": "agent", "relationship": "peer", "line": {"display_name": None, "uid": "ln_bare", "provider_key": "+15550000077"}}
HOME = "cht_home"
SOLO = "cht_solo"  # the owner alone with the line


@pytest.fixture(autouse=True)
def quick_reconnect(monkeypatch):
    monkeypatch.setattr(plow_chat, "RECONNECT_DELAY", 0.05)
    monkeypatch.setattr(plow_chat, "BURST_SECONDS", 0.1)


async def eventually(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def approve(prompt):
    return "SEND"


def run_bridge(scenario, fake=None, cursor=None):
    """Run `scenario(fake, bridge, delivered)` with the bridge's socket
    supervisor live against `fake` (a fresh one by default). Pass the same
    fake and cursor twice to restart the server against the same chat."""
    fake = fake or FakePlow()
    cursor = cursor or Path(tempfile.mkdtemp()) / "last_seen.json"

    async def main():
        base_url, server = await fake.serve()
        delivered = []

        async def deliver(content, meta):
            delivered.append((content, meta))

        async with httpx.AsyncClient(base_url=base_url, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
            bridge = Bridge(client, deliver, cursor, reviewer=approve)
            supervisor = asyncio.create_task(bridge.run())
            await eventually(lambda: fake.sockets)
            try:
                await scenario(fake, bridge, delivered)
            finally:
                supervisor.cancel()
                server.should_exit = True
    asyncio.run(main())


@pytest.mark.parametrize("handle, name", [(TEAMMATE, "Alex"), (STRANGER, STRANGER), (SPRUCE, "Spruce"),
                                          (NAMELESS, "+15550000077")],
                         ids=["named", "unnamed", "peer-agent", "unnamed-peer-agent"])
def test_any_participant_is_delivered_by_name_without_the_chats_history(handle, name):
    async def scenario(fake, bridge, delivered):
        await fake.inbound(HOME, handle, "can you look at the laptop?")
        await eventually(lambda: delivered)
        await fake.drop_sockets()  # the catch-up after reconnecting must not replay it either
        await eventually(lambda: fake.sockets)
        await fake.inbound(HOME, OWNER, "marker")
        await eventually(lambda: len(delivered) >= 2)
        await asyncio.sleep(0.2)
        assert delivered == [("can you look at the laptop?", {"from": name, "chat": HOME}),
                             ("marker", {"from": "Jordan", "chat": HOME})]

    fake = FakePlow()
    fake.add(HOME, OWNER, "said before the sous was listening")
    run_bridge(scenario, fake)


@pytest.mark.parametrize("trusted_chats, marked", [
    (None, {}),  # no allowlist: every chat carries authority
    (f"{HOME}  # owner 1:1\n", {}),
    ("cht_owners\n", {"trusted": "false"}),
], ids=["no-allowlist", "listed", "unlisted"])
def test_only_allowlisted_chats_carry_authority(trusted_chats, marked):
    cursor = Path(tempfile.mkdtemp()) / "last_seen.json"
    if trusted_chats is not None:
        (cursor.parent / plow_chat.TRUSTED_CHATS).write_text(trusted_chats)

    async def scenario(fake, bridge, delivered):
        await fake.inbound(HOME, TEAMMATE, "unlock the front door")
        await eventually(lambda: delivered)
        assert delivered == [("unlock the front door", {"from": "Alex", "chat": HOME} | marked)]
    run_bridge(scenario, cursor=cursor)


def test_a_reconnect_inside_a_new_chats_first_burst_never_replays_its_history(monkeypatch):
    monkeypatch.setattr(plow_chat, "BURST_SECONDS", 0.5)

    async def scenario(fake, bridge, delivered):
        await fake.inbound(HOME, OWNER, "fresh")
        await asyncio.sleep(0.05)  # heard, but its burst is still open
        await fake.drop_sockets()
        await eventually(lambda: delivered)
        await asyncio.sleep(0.3)
        assert [c for c, _ in delivered] == ["fresh"]

    fake = FakePlow()
    fake.add(HOME, OWNER, "said before the sous was listening")
    run_bridge(scenario, fake)


def test_reply_echo_system_notes_and_blank_texts_never_reach_the_sous():
    async def scenario(fake, bridge, delivered):
        await bridge.reply(HOME, "on it")
        assert fake.sent == [(HOME, "on it")]
        await fake.inbound(HOME, PLOW, "Spruce joined the chat")
        await fake.inbound(HOME, SPRUCE, "")  # a tapback
        await asyncio.sleep(0.2)
        await fake.inbound(HOME, OWNER, "marker")
        await eventually(lambda: delivered)
        assert [c for c, _ in delivered] == ["marker"]
    run_bridge(scenario)


@pytest.mark.parametrize("unread", ["unheard", "in-burst", "during-the-check"])
def test_a_reply_is_refused_while_newer_texts_are_unread(monkeypatch, unread):
    """A text the sous hasn't been handed yet, whether the socket missed it or
    its burst is still open, stops the reply and reaches the sous instead."""
    monkeypatch.setattr(plow_chat, "BURST_SECONDS", 30)

    async def scenario(fake, bridge, delivered):
        await fake.inbound(HOME, OWNER, "how's the demo?")
        await eventually(lambda: bridge.burst)
        await bridge.flush()  # the sous has read it
        if unread == "unheard":
            fake.add(HOME, TEAMMATE, "and when can we show it?")
        elif unread == "during-the-check":  # the socket takes it while the check reads the chat
            read, once = bridge.client.get, []

            async def get(*args, **kwargs):
                r = await read(*args, **kwargs)
                if not once:
                    once.append(1)
                    await fake.inbound(HOME, TEAMMATE, "and when can we show it?")
                    await eventually(lambda: bridge.burst)
                return r
            monkeypatch.setattr(bridge.client, "get", get)
        else:
            await fake.inbound(HOME, TEAMMATE, "and when can we show it?")
            await eventually(lambda: bridge.burst)
        assert (await bridge.reply(HOME, "build 47 is out")).startswith("not sent")
        assert fake.sent == []
        assert delivered[-1] == ("and when can we show it?", {"from": "Alex", "chat": HOME})
        assert (await bridge.reply(HOME, "build 47 is out; Thursday works")).startswith("sent")
        assert fake.sent == [(HOME, "build 47 is out; Thursday works")]
    run_bridge(scenario)


@pytest.mark.parametrize("flaky", [False, True], ids=["normal", "503-then-retry"])
def test_reconnect_catches_up_without_duplicates(flaky):
    async def scenario(fake, bridge, delivered):
        await fake.inbound(HOME, OWNER, "before")
        await eventually(lambda: delivered)
        await fake.drop_sockets()
        fake.add(HOME, OWNER, "during the gap")
        if flaky:
            fake.flaky.add(HOME)  # the first catch-up read answers 503
        await eventually(lambda: len(delivered) == 2)
        await fake.inbound(HOME, OWNER, "after")
        await eventually(lambda: len(delivered) == 3)
        await asyncio.sleep(0.2)
        assert [c for c, _ in delivered] == ["before", "during the gap", "after"]
    run_bridge(scenario)


def test_restart_delivers_texts_sent_while_down_exactly_once(tmp_path):
    fake, cursor = FakePlow(), tmp_path / "last_seen.json"

    async def before_restart(fake, bridge, delivered):
        await fake.inbound(HOME, OWNER, "before restart")
        await eventually(lambda: delivered)

    async def after_restart(fake, bridge, delivered):
        await eventually(lambda: delivered)
        await asyncio.sleep(0.2)
        assert [c for c, _ in delivered] == ["while down"]

    run_bridge(before_restart, fake, cursor)
    fake.add(HOME, OWNER, "while down")
    run_bridge(after_restart, fake, cursor)
    assert cursor.stat().st_mode & 0o777 == 0o600


def test_a_late_text_never_replays_an_earlier_one_after_restart(tmp_path):
    fake, cursor = FakePlow(), tmp_path / "last_seen.json"

    async def before_restart(fake, bridge, delivered):
        await fake.inbound(HOME, OWNER, "sent second, landed first")
        await fake.inbound(HOME, TEAMMATE, "sent first, landed second", late=True)
        await eventually(lambda: len(delivered) == 2)

    async def after_restart(fake, bridge, delivered):
        await fake.inbound(HOME, OWNER, "marker")
        await eventually(lambda: delivered)
        await asyncio.sleep(0.2)
        assert [c for c, _ in delivered] == ["marker"]

    run_bridge(before_restart, fake, cursor)
    run_bridge(after_restart, fake, cursor)


@pytest.mark.parametrize("live", [True, False], ids=["socket", "catch-up"])
def test_one_speakers_back_to_back_texts_reach_the_sous_as_one(live, tmp_path):
    fake, cursor = FakePlow(), tmp_path / "last_seen.json"
    texts = [(OWNER, "add this to the page"), (OWNER, "https://example.com/paper"), (TEAMMATE, "me too")]

    async def first_contact(fake, bridge, delivered):  # catch-up reads only chats the bridge has heard from
        await fake.inbound(HOME, OWNER, "hi")
        await eventually(lambda: delivered)

    async def burst(fake, bridge, delivered):
        if live:
            for handle, body in texts:
                await fake.inbound(HOME, handle, body)
        await eventually(lambda: len(delivered) >= 2)
        await asyncio.sleep(0.3)
        assert [(c, m["from"]) for c, m in delivered] == [
            ("add this to the page\n\nhttps://example.com/paper", "Jordan"), ("me too", "Alex")]

    run_bridge(first_contact, fake, cursor)
    if not live:
        for handle, body in texts:
            fake.add(HOME, handle, body)
    run_bridge(burst, fake, cursor)


PHOTO = b"\xff\xd8\xff fake jpeg"


PLOW_AUTH = f"Bearer {TOKEN}"


@pytest.mark.parametrize("texts, files, content, saved, files_host, auth", [
    (["The spacing sucks. See screenshot"], [("IMG_5570.jpeg", PHOTO)], "The spacing sucks. See screenshot", [PHOTO],
     None, PLOW_AUTH),
    # Served from another origin, the same bytes arrive but the token stays home.
    ([""], [("IMG_5571.jpeg", PHOTO)], "", [PHOTO], "localhost", None),
    (["two shots", ""], [("a.jpeg", PHOTO), ("b.png", b"png")], "two shots", [PHOTO, b"png"], None, PLOW_AUTH),
    (["see this"], [("gone.jpeg", None)], "see this\n\n(attachment gone.jpeg couldn't be fetched: HTTPStatusError)", [],
     None, PLOW_AUTH),
], ids=["captioned-photo", "bare-photo-other-host", "burst-of-two", "unfetchable"])
def test_a_members_attachments_reach_the_sous_as_local_files(texts, files, content, saved, files_host, auth):
    async def scenario(fake, bridge, delivered):
        fake.files_host = files_host
        for i, text in enumerate(texts):
            await fake.inbound(HOME, OWNER, text, files=files[i:i + 1] if len(texts) > 1 else files)
        await eventually(lambda: delivered)
        await asyncio.sleep(0.2)
        [(got, meta)] = delivered
        assert (got, meta["from"]) == (content, "Jordan")
        paths = [Path(p) for p in meta["file"].split()] if saved else []
        assert "file" in meta if saved else "file" not in meta
        assert [p.read_bytes() for p in paths] == saved
        assert all(p.parent.stat().st_mode & 0o777 == 0o700 for p in paths)
        assert list(fake.files_auth.values()) == [auth] * len(files)
    run_bridge(scenario)


@pytest.mark.parametrize("field, content, saved", [
    ("url", "see this\n\n(attachment shot.jpeg isn't available)", False),
    ("filename", "see this", True),
], ids=["no-url", "no-filename"])
def test_an_attachment_missing_a_field_never_holds_up_delivery(field, content, saved):
    async def scenario(fake, bridge, delivered):
        message = fake.add(HOME, OWNER, "see this", files=[("shot.jpeg", PHOTO)])
        message["attachments"][0][field] = None
        await fake._broadcast(message)
        await eventually(lambda: delivered)
        got, meta = delivered[0]
        assert got == content
        assert ("file" in meta) == saved
        if saved:
            [path] = meta["file"].split()
            assert Path(path).read_bytes() == PHOTO
        await asyncio.sleep(0.2)
        await fake.inbound(HOME, OWNER, "marker")
        await eventually(lambda: len(delivered) == 2)
        assert delivered[1][0] == "marker"
    run_bridge(scenario)


@pytest.mark.parametrize("written", [
    f"{TOKEN}\n",  # latch agents new --own --token-file
    f"PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN={TOKEN}\n# plow-agent-uid: 73f5\n",  # older --credential-file
])
def test_reads_the_credential_file_latch_agents_new_writes(tmp_path, written):
    (tmp_path / CREDENTIALS).write_text(written)
    assert read_credentials(tmp_path / CREDENTIALS) == {"PLOW_API_BASE": "https://api.plow.co", "PLOW_AGENT_TOKEN": TOKEN}


@pytest.fixture
def shot(tmp_path):
    file = tmp_path / "shot.png"
    file.write_bytes(b"\x89PNG fake image")
    return file


def test_send_file_uploads_without_the_plow_token_and_sends_the_attachment(shot):
    async def scenario(fake, bridge, delivered):
        result = await call(bridge, "send_file", {"chat": HOME, "paths": [str(shot)], "caption": "the bug"})

        (uid, attachment), = fake.attachments.items()
        assert (attachment["filename"], attachment["content_type"]) == ("shot.png", "image/png")
        assert attachment["size_bytes"] == shot.stat().st_size
        uploaded, headers = attachment["upload"]
        assert uploaded == shot.read_bytes()
        assert "authorization" not in headers
        assert headers["x-provider-grant"] == uid  # every returned header goes back
        assert attachment["sent_with"] == "the bug"
        assert result.startswith("sent msg_")
        await asyncio.sleep(0.1)
        assert delivered == []  # the echo of our own send stays out of the sous
    run_bridge(scenario)


def test_send_file_needs_no_caption(shot):
    async def scenario(fake, bridge, delivered):
        await call(bridge, "send_file", {"chat": HOME, "paths": [str(shot)]})
        assert fake.sent == [(HOME, "")]
    run_bridge(scenario)


@pytest.fixture
def shots(tmp_path):
    def make(count):
        files = [tmp_path / f"shot{n}.png" for n in range(count)]
        for n, file in enumerate(files):
            file.write_bytes(b"image %d" % n)
        return [str(file) for file in files]
    return make


def test_four_files_go_as_one_message_in_order(shots):
    async def scenario(fake, bridge, delivered):
        await call(bridge, "send_file", {"chat": HOME, "paths": shots(4), "caption": "all four"})

        assert fake.sent == [(HOME, "all four")]
        assert [fake.attachments[uid]["filename"] for uid in fake.sent_uids[0]] == [f"shot{n}.png" for n in range(4)]
        assert [fake.attachments[uid]["upload"][0] for uid in fake.sent_uids[0]] == [b"image %d" % n for n in range(4)]
    run_bridge(scenario)


def test_one_failed_upload_sends_nothing(shots):
    async def scenario(fake, bridge, delivered):
        fake.refused_upload = "shot1.png"
        with pytest.raises(RuntimeError, match="shot1.png.*nothing sent"):
            await bridge.send_file(HOME, shots(3))
        assert fake.sent == []
        assert len(fake.attachments) == 2  # the third was never declared
    run_bridge(scenario)


def test_one_bad_file_fails_before_any_reaches_plow(shots, tmp_path):
    async def scenario(fake, bridge, delivered):
        with pytest.raises(ValueError, match="gone.png"):
            await bridge.send_file(HOME, [*shots(2), str(tmp_path / "gone.png")])
        assert fake.attachments == {}
    run_bridge(scenario)


@pytest.mark.parametrize("count", [0, 5])
def test_a_message_takes_one_to_four_files(shots, count):
    async def scenario(fake, bridge, delivered):
        with pytest.raises(ValueError, match=f"{count} files"):
            await bridge.send_file(HOME, shots(count))
        assert fake.attachments == {}
    run_bridge(scenario)


@pytest.mark.parametrize("status", [403, 302])
def test_a_failed_upload_is_never_sent_and_keeps_the_signed_url_out_of_the_error(shot, status):
    async def scenario(fake, bridge, delivered):
        fake.upload_status = status
        with pytest.raises(RuntimeError, match=f"HTTP {status}") as failure:
            await bridge.send_file(HOME, [str(shot)])
        assert SIGNATURE not in str(failure.value)
        assert fake.sent == []
    run_bridge(scenario)


@pytest.mark.parametrize("send", [
    lambda bridge, file: bridge.reply(HOME, "on it"),
    lambda bridge, file: bridge.send_file(HOME, [str(file)]),
], ids=["reply", "send_file"])
def test_a_held_send_is_a_failure(shot, send):
    async def scenario(fake, bridge, delivered):
        fake.hold = True
        with pytest.raises(RuntimeError, match="never deliver"):
            await send(bridge, shot)
    run_bridge(scenario)


def test_a_type_plow_refuses_says_why(tmp_path):
    file = tmp_path / "movie.txt"
    file.write_text("words")

    async def scenario(fake, bridge, delivered):
        with pytest.raises(RuntimeError, match="movie.txt.*415.*unsupported_content_type"):
            await bridge.send_file(HOME, [str(file)])
        assert fake.sent == []
    run_bridge(scenario)


@pytest.mark.parametrize("name, size, why", [
    ("huge.png", plow_chat.MAX_ATTACHMENT_BYTES + 1, "100 MiB"),
    ("empty.png", 0, "100 MiB"),
    ("mystery.xyzzy", 5, "file type"),
])
def test_a_file_plow_cannot_take_fails_before_reaching_plow(tmp_path, name, size, why):
    file = tmp_path / name
    with file.open("wb") as f:
        f.truncate(size)  # sparse, so the oversized case costs no disk

    async def scenario(fake, bridge, delivered):
        with pytest.raises(ValueError, match=why):
            await bridge.send_file(HOME, [str(file)])
        assert fake.attachments == {}
    run_bridge(scenario)


def test_a_missing_file_fails_clearly(tmp_path):
    async def scenario(fake, bridge, delivered):
        with pytest.raises(ValueError, match="not a file"):
            await bridge.send_file(HOME, [str(tmp_path / "gone.png")])
    run_bridge(scenario)


@pytest.mark.parametrize("name, expected", [
    ("a.PNG", "image/png"),
    ("a.jpg", "image/jpeg"),
    ("a.heic", "image/heic"),
    ("a.webp", "image/webp"),
    ("a.ico", "image/x-icon"),
    ("a.pdf", "application/pdf"),
    ("a.vcf", "text/vcard"),
    ("a.wav", "audio/x-wav"),  # Python 3.14 guesses audio/vnd.wave
    ("a.avi", "video/x-msvideo"),  # Python 3.14 guesses video/vnd.avi
    ("a.midi", "audio/midi"),
    ("a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("a.tar.gz", "application/x-gzip"),
])
def test_declares_the_type_plow_names(name, expected):
    assert content_type_of(Path(name)) == expected


def test_only_named_tools_reach_the_bridge():
    async def scenario(fake, bridge, delivered):
        with pytest.raises(ValueError, match="unknown tool run"):
            await call(bridge, "run", {})
        assert (await call(bridge, "reply", {"chat": HOME, "text": "hi"})).startswith("sent ")
    run_bridge(scenario)


def test_start_chat_sends_the_first_message_and_hears_the_answer_in_the_new_chat():
    async def scenario(fake, bridge, delivered):
        started = await call(bridge, "start_chat", {"handles": [TEAMMATE], "text": "hi Alex, it's Cedar"})
        again = await call(bridge, "start_chat", {"handles": [TEAMMATE], "text": "one more thing"})
        chat = started.removeprefix("started ")
        assert again == f"sent in existing chat {chat}"
        assert [(m, b) for m, b, _ in fake.started] == [([TEAMMATE], "hi Alex, it's Cedar"), ([TEAMMATE], "one more thing")]
        assert fake.started[0][2] != fake.started[1][2]  # a fresh key per send, so Plow sends the second
        await fake.inbound(chat, TEAMMATE, "hey Cedar")
        await eventually(lambda: delivered)
        assert delivered == [("hey Cedar", {"from": "Alex", "chat": chat})]
    run_bridge(scenario)


def test_an_answer_to_a_chat_started_while_the_socket_is_down_is_caught_up_on_reconnect(monkeypatch):
    async def scenario(fake, bridge, delivered):
        monkeypatch.setattr(plow_chat, "RECONNECT_DELAY", 0.5)
        await fake.drop_sockets()  # nobody hears the echo or the answer live
        fake.answer = (TEAMMATE, "hey Cedar")  # in before the bridge reads the new chat back
        chat = (await bridge.start_chat([TEAMMATE], "hi Alex")).removeprefix("started ")
        fake.add(chat, TEAMMATE, "and later")
        await eventually(lambda: delivered)
        assert delivered == [("hey Cedar\n\nand later", {"from": "Alex", "chat": chat})]
    run_bridge(scenario)


@pytest.mark.parametrize("handles, text, error", [
    ([TEAMMATE], "  ", "empty"),
    ([], "hello", "Plow refused"),
    ([TEAMMATE, STRANGER], "hello", STRANGER),
], ids=["empty-text", "plow-refuses", "stranger"])
def test_start_chat_fails_loudly(handles, text, error):
    async def scenario(fake, bridge, delivered):
        with pytest.raises((ValueError, RuntimeError), match=re.escape(error)):
            await bridge.start_chat(handles, text)
        assert fake.started == []
    run_bridge(scenario)


def test_start_chat_reaches_allowlisted_handles_never_marks_chats_trusted_and_caps_new_chats(monkeypatch):
    monkeypatch.setattr(plow_chat, "NEW_CHATS_PER_DAY", 2)
    cursor = Path(tempfile.mkdtemp()) / "last_seen.json"
    allowed = ["+15550000101", "+15550000102", "+15550000103"]
    (cursor.parent / plow_chat.START_CHAT_ALLOW).write_text("\n".join(allowed) + "  # new hires\n")

    async def scenario(fake, bridge, delivered):
        await bridge.start_chat([allowed[0]], "welcome")
        await bridge.start_chat([allowed[0]], "and again")  # the same chat adopted: not a new one
        await bridge.start_chat([allowed[1]], "welcome")
        with pytest.raises(ValueError, match="already started 2 new chats today"):
            await bridge.start_chat([allowed[2]], "welcome")
        assert [m for m, _, _ in fake.started] == [[allowed[0]], [allowed[0]], [allowed[1]]]
        assert not any("trusted" in body for body in fake.start_bodies)  # Plow would honor it from us
        log = (cursor.parent / plow_chat.STARTED_CHATS).read_text().splitlines()
        assert [line.split()[2:] for line in log] == [["created", allowed[0]], ["adopted", allowed[0]],
                                                      ["created", allowed[1]]]
    run_bridge(scenario, cursor=cursor)


def test_parallel_start_chats_share_the_daily_cap(monkeypatch):
    monkeypatch.setattr(plow_chat, "NEW_CHATS_PER_DAY", 1)
    cursor = Path(tempfile.mkdtemp()) / "last_seen.json"
    (cursor.parent / plow_chat.START_CHAT_ALLOW).write_text("+15550000101\n+15550000102\n")

    async def scenario(fake, bridge, delivered):
        results = await asyncio.gather(*(bridge.start_chat([h], "welcome") for h in ["+15550000101", "+15550000102"]),
                                       return_exceptions=True)
        assert len(fake.started) == 1
        assert sum(isinstance(r, ValueError) for r in results) == 1
    run_bridge(scenario, cursor=cursor)


def test_an_answer_to_a_new_chat_whose_first_read_fails_is_caught_up_on_reconnect(monkeypatch):
    async def scenario(fake, bridge, delivered):
        monkeypatch.setattr(plow_chat, "RECONNECT_DELAY", 0.5)
        await fake.drop_sockets()
        fake.answer = (TEAMMATE, "hey Cedar")
        fake.flaky.add("cht_started0")  # Plow sends, then the bridge's first read of the chat fails
        with pytest.raises(httpx.HTTPStatusError):
            await bridge.start_chat([TEAMMATE], "hi Alex")
        await eventually(lambda: delivered)
        assert delivered == [("hey Cedar", {"from": "Alex", "chat": "cht_started0"})]
    run_bridge(scenario)


def reviewing(answer, prompts):
    """A reviewer that records each prompt and answers `answer`."""
    async def reviewer(prompt):
        prompts.append(prompt)
        return answer
    return reviewer


@pytest.mark.parametrize("chat, answer, outcome", [
    ("cht_solo", "HOLD: never asked", "sent"),
    (HOME, "SEND", "sent"),
    (HOME, "**SEND.**", "sent"),
    (HOME, "SEND: nothing contradicts it", "sent"),
    (HOME, "HOLD: Jordan just said Tuesday doesn't work", "held"),
    (HOME, "Looks fine to me", "error"),
], ids=["owner-only-unreviewed", "send", "send-decorated", "send-with-reason", "hold", "no-verdict"])
def test_a_text_anyone_but_the_owner_reads_goes_out_only_past_the_reviewer(chat, answer, outcome):
    """The reviewer reads the thread fresh from Plow, so the owner's newest
    text counts even when the sous was handed it mid-turn and never read it."""
    prompts = []

    async def scenario(fake, bridge, delivered):
        bridge.reviewer = reviewing(answer, prompts)
        await fake.inbound(chat, OWNER, "Tuesday doesn't work for me")
        await eventually(lambda: delivered)
        if outcome == "error":
            with pytest.raises(RuntimeError, match="neither SEND nor HOLD"):
                await bridge.reply(chat, "I'll keep Tuesday's logistics straight")
        else:
            result = await bridge.reply(chat, "I'll keep Tuesday's logistics straight")
            assert result.startswith("not sent: a reviewer held it for existing chat cht_home with Alex (Jordan just said") if outcome == "held" else result.startswith("sent")
        assert fake.sent == ([(chat, "I'll keep Tuesday's logistics straight")] if outcome == "sent" else [])
        if chat == HOME:
            assert "Jordan: Tuesday doesn't work for me" in prompts[0]
            assert "working for Jordan is about to send the iMessage below to Alex." in prompts[0]
        else:
            assert prompts == []
    run_bridge(scenario)


def test_the_reviewer_sees_which_file_was_sent_not_just_that_one_was():
    """Plow sends a line's contact card as a bodiless file; the reviewer must
    see its name, or it reads as the line having already answered with one."""
    prompts = []

    async def scenario(fake, bridge, delivered):
        bridge.reviewer = reviewing("SEND", prompts)
        await fake.inbound(HOME, OWNER, "can you share the log?")
        await eventually(lambda: delivered)
        fake.add(HOME, None, "", files=[("cedar.vcf", b"BEGIN:VCARD")])
        assert (await bridge.reply(HOME, "will do once it's cleared")).startswith("sent")
        assert "you: (cedar.vcf)" in prompts[0]
    run_bridge(scenario)


REFUSED = "not sent: a text to the head chef names the Swarm agent you consulted first"


@pytest.mark.parametrize("gate, kwargs, outcome", [
    (False, {}, "can I mint the invite?"),
    (True, {}, REFUSED),
    (True, {"consulted": "@cedar:swarm.example"}, "can I mint the invite?\n\n— with cedar"),
    (True, {"consulted": "@line:swarm.example"}, REFUSED),
    (True, {"consulted": "@owner:swarm.example"}, REFUSED),
    (True, {"consulted": "Cedar"}, REFUSED),
    (True, {"dangerously_skip_attaching_a_priority": True, "priority": "!room/p1"}, "can I mint the invite?"),
], ids=["gate-off", "none-named", "consulted", "itself", "the-owner", "not-a-user-id", "skip-flag"])
def test_a_text_to_the_owner_alone_names_the_agent_consulted_first(gate, kwargs, outcome):
    """With `consult-gate` set, every text in the owner-only chat, an answer
    to them included, names another Swarm agent it was worked with, and
    carries that credit; the skip flag sends it bare."""
    async def scenario(fake, bridge, delivered):
        await fake.inbound("cht_solo", OWNER, "what's the status?")
        await eventually(lambda: delivered)
        bridge.consult = ("@line:swarm.example", "@owner:swarm.example") if gate else ()
        result = await bridge.reply("cht_solo", "can I mint the invite?", **kwargs)
        assert result.startswith("not sent" if outcome == REFUSED else "sent"), result
        assert result.startswith(outcome) if outcome == REFUSED else fake.sent[-1] == ("cht_solo", outcome)
    run_bridge(scenario)


@pytest.mark.parametrize("profiles, credit", [
    ({"old": None, "hq": "Aspen ⚉ Kim's front desk"}, "Aspen ⚉ Kim's front desk"),
    ({"old": None, "hq": None}, "abcdefghij"),
    ({"old": None, "hq": {}}, "abcdefghij"),
], ids=["named-on-another-server", "unknown-everywhere", "no-display-name"])
def test_a_credit_shows_the_consulted_agents_swarm_display_name(monkeypatch, profiles, credit):
    """The credit names the agent as Swarm shows it, from whichever of the
    line's servers knows it, and by its localpart when none names it."""
    def profile(account, path):
        assert path == "/profile/%40abcdefghij%3Aexample.com"
        found = profiles[account["user_id"]]
        if found is None:
            raise HTTPError(path, 404, "Profile not found", None, None)
        return found if isinstance(found, dict) else {"displayname": found}
    monkeypatch.setattr(plow_chat, "matrix_get", profile)

    async def scenario(fake, bridge, delivered):
        await fake.inbound("cht_solo", OWNER, "what's the status?")
        await eventually(lambda: delivered)
        bridge.name_of = lambda user_id: plow_chat.display_name([{"user_id": "old"}, {"user_id": "hq"}], user_id)
        assert (await bridge.reply("cht_solo", "merged", consulted="@abcdefghij:example.com")).startswith("sent")
        assert fake.sent[-1] == ("cht_solo", f"merged\n\n— with {credit}")
    run_bridge(scenario)


def test_start_chat_and_send_file_are_reviewed_too(shot):
    prompts = []

    async def scenario(fake, bridge, delivered):
        bridge.reviewer = reviewing("HOLD: Alex is in the room", prompts)
        await fake.inbound(HOME, OWNER, "Tuesday's out")
        await eventually(lambda: delivered)
        assert (await bridge.start_chat([TEAMMATE], "hi Alex")).startswith("not sent: a reviewer held it")
        assert (await bridge.send_file(HOME, [str(shot)], "the build")).startswith("not sent: a reviewer held it")
        assert (await bridge.reply(HOME, "the build", consulted="@cedar:x")).startswith("not sent: `consulted` credits")
        assert fake.started == [] and fake.sent == []
        assert ["hi Alex" in prompts[0], "the build" in prompts[1]] == [True, True]
        assert "Jordan: Tuesday's out" in prompts[0]  # start_chat reviews against the chat Plow will adopt
        (bridge.cursor.parent / plow_chat.TRUSTED_CHATS).write_text(f"{HOME}\n")  # a company thread
        assert (await bridge.reply(HOME, "the build")).startswith("sent") and len(prompts) == 2
    run_bridge(scenario)


def test_a_text_landing_while_the_reviewer_thinks_stops_the_send():
    async def scenario(fake, bridge, delivered):
        async def slow_reviewer(prompt):
            await fake.inbound(HOME, TEAMMATE, "actually, skip Tuesday")
            await eventually(lambda: delivered)  # handed over already, so only a fresh read catches it
            return "SEND"
        bridge.reviewer = slow_reviewer
        assert (await bridge.reply(HOME, "see you Tuesday")).startswith("not sent: 1 newer message(s) landed")
        assert fake.sent == []
    run_bridge(scenario)


@pytest.mark.parametrize("agent, send, refused", [
    ("sous", {"room": "#dev:x", "text": "merged\n\n— with zelda"}, False),
    ("sous", {"room": "@owner:x", "text": "merged"}, False),
    ("sous", {"room": "@owner:x", "text": "merged\n\n— with none"}, False),
    ("sous", {"room": "#dev:x", "mentions": ["@owner:x"], "text": "merged\n\n— with oak"}, False),
    ("sous", {"room": "@owner:x", "text": "merged\n\n— with Birch"}, False),
    ("sous", {"room": "@owner:x", "text": "merged\n\n— with elm"}, True),
    ("sous", {"room": "@owner:x", "text": "merged\n\n— with zelda"}, True),
    ("sous", {"room": "@owner:x", "mentions": ["@zelda:x"], "text": "ask zelda\n\n— with zelda"}, True),
    ("sous", {"room": "@owner:x", "text": "retry\n\n— with zelda"}, True),
    ("sous", {"room": "@owner:x", "text": "merged\n\n— with cedar"}, True),
    ("sous", {"room": "@owner:x", "text": "merged\n\n— with owner"}, True),
    ("sous", {"room": "@owner:x", "text": "merged\n\n— with Owner"}, True),
    ("sous", {"room": "@owner:x", "text": "merged\n\n— with Fake"}, True),
    ("sous", {"room": "@owner:x", "text": "merged\n\n— with zelda", "dangerously_skip_attaching_a_priority": True}, False),
    ("sous", {"room": "!dmOwner", "text": "merged\n\n— with zelda"}, True),
    ("sous", {"room": "!dmOwner", "text": "merged\n\n— with oak"}, False),
    ("sous", {"room": "!dmOther", "text": "merged\n\n— with zelda"}, False),
    ("sous", {"room": "!notIn", "text": "merged\n\n— with zelda"}, False),
    ("dev", {"room": "@owner:x", "text": "merged\n\n— with dev"}, True),
    ("dev", {"room": "@owner:x", "text": "merged\n\n— with cedar"}, False),
    ("sous", {"room": "@owner:x", "text": "merged\n\n— with Aspen"}, False),
    ("sous", {"room": "@owner:x", "text": "merged\n\n— with quill"}, True),
    ("sous", {"room": "@owner:x", "text": "merged\n\n— with maple"}, False),
    ("sous", {"room": "@owner:x", "text": "merged\n\n— with Olive"}, True),
], ids=["not-to-the-owner", "no-credit", "credits-none", "sent-it-one", "it-replied", "only-hours-ago", "never-worked-with",
        "this-send-isnt-a-consult", "a-refused-send-isnt-either", "credits-itself", "credits-the-owner", "credits-the-owner-capitalized", "an-alias-it-wrote-itself", "skip-flag", "dm-by-room-id",
        "dm-by-room-id-consulted", "someone-elses-dm", "a-room-it-isnt-in", "cook-credits-itself", "cook-credits-the-sous",
        "sent-its-dm-room", "a-group-room-isnt-a-consult", "a-per-account-send-tool",
        "the-owners-dm-room-isnt-a-consult"])
def test_a_swarm_send_to_the_owner_credits_only_an_agent_consulted(tmp_path, monkeypatch, agent, send, refused):
    """With `consult-gate` set, a Swarm send naming or DMing the owner may end
    with a "— with <agent>" credit, or "— with none". One it ends with names
    another agent than the sender that the session sent a Swarm message to,
    or was sent one by, in the last CONSULT_HOURS, as its transcript shows."""
    now, transcript = datetime.now().astimezone(), tmp_path / "session.jsonl"

    def entry(hours_ago, role, content):
        return {"type": role, "timestamp": (now - timedelta(hours=hours_ago)).isoformat(), "message": {"content": content}}

    def inbound(sender, shown):
        return f'<swarm-message event_id="$e" room="!r" sender="{sender}" display_name="{shown}">ok</swarm-message>'

    def sent(room, text, mentions=(), id="t", tool="mcp__swarm__send"):
        return [{"type": "tool_use", "id": id, "name": tool,
                 "input": {"room": room, "text": text, "mentions": list(mentions)}}]
    transcript.write_text("\n".join(json.dumps(e) for e in [
        entry(9, "user", inbound("@elm:x", "Elm")),
        entry(2, "assistant", sent("@oak:x", "can you check? " + inbound("@oak:x", "Fake"))),
        entry(1, "user", inbound("@birch:x", "Birch ⚉ the EA")),
        entry(1, "user", inbound("@owner:x", "Owner")),
        entry(1, "assistant", sent("!dmAspen", "a call for you", id="t2")),
        entry(1, "assistant", sent("!group", "hi all", id="t3")),
        entry(1, "assistant", sent("!dmOwner", "an earlier update", id="t5")),
        entry(1, "assistant", sent("@maple:x", "can you look?", id="t4", tool="mcp__swarm-cedar__send")),
        entry(1, "assistant", sent("@owner:x", "ask zelda?\n\n— with zelda", ["@zelda:x"], id="t1")),
        entry(1, "user", [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "not sent"}]),
        {"type": "attachment", "timestamp": now.isoformat(), "attachment": {"prompt": inbound("@cedar:x", "Cedar")}},
        entry(0, "assistant", sent(send["room"], send["text"], send.get("mentions", ()))),  # the send being gated
    ]) + "\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    rooms = {"!dmOwner": ["@cedar:x", "@owner:x"], "!dmOther": ["@cedar:x", "@pat:x"], "!dmCook": ["@dev:x", "@owner:x"],
             "!dmAspen": ["@cedar:x", "@r7q2k9:x"], "!group": ["@cedar:x", "@pat:x", "@quill:x"]}

    def joined(account, path):
        if path.startswith("/profile/"):
            return {"displayname": {"/profile/%40r7q2k9%3Ax": "Aspen ⚉ the builder", "/profile/%40owner%3Ax": "Olive"}.get(path, "")}
        room = unquote(path.split("/")[2])
        if room not in rooms:
            raise HTTPError(path, 403, "not in the room", None, None)
        return {"joined": dict.fromkeys(rooms[room], {})}
    monkeypatch.setattr(plow_chat, "matrix_get", joined)
    (tmp_path / "cred.json").write_text(json.dumps({"user_id": "@cedar:x"}))
    swarm = plow_chat.state_dir("boss") / "swarm"
    swarm.mkdir(parents=True)
    (swarm / "credential.json").symlink_to(tmp_path / "cred.json")
    cook = tmp_path / ".swarm/accounts/x/dev/credential.json"
    cook.parent.mkdir(parents=True)
    cook.write_text(json.dumps({"user_id": "@dev:x"}))
    (plow_chat.state_dir("boss") / "cooks").mkdir()
    (plow_chat.state_dir("boss") / "cooks" / "dev.mcp").write_text(json.dumps(
        {"mcpServers": {"swarm": {"command": "swarm", "args": ["mcp", "claude", "--account", "@dev:x"]}}}))
    gate = plow_chat.plow_chat_dir("boss")
    gate.mkdir(parents=True)
    assert plow_chat.swarm_send_refusal("boss", agent, send, str(transcript)) is None  # no gate, no refusal
    (gate / plow_chat.CONSULT_GATE).write_text("@owner:x\n")
    assert (plow_chat.swarm_send_refusal("boss", agent, send, str(transcript)) is not None) == refused


def test_an_expired_claude_login_tells_the_owner_once_until_a_turn_ends_normally(monkeypatch, tmp_path):
    """The session whose login expired can't speak, so its StopFailure hook
    tells the owner on the line; the sous and a cook failing together tell once."""
    fake, ready = FakePlow(), threading.Event()

    def serve():
        async def main():
            fake.base_url, server = await fake.serve()
            ready.set()
            while not server.should_exit:
                await asyncio.sleep(0.05)
        asyncio.run(main())
    threading.Thread(target=serve, daemon=True).start()
    ready.wait(5)
    monkeypatch.setenv("HOME", str(tmp_path))
    line = tmp_path / ".claude-kitchen" / "risotto" / "plow-chat"
    line.mkdir(parents=True)
    (line / CREDENTIALS).write_text(f"PLOW_API_BASE={fake.base_url}\nPLOW_AGENT_TOKEN={TOKEN}\n")
    monkeypatch.setenv("AGENT_KITCHEN", "risotto")
    monkeypatch.setenv("STATUS_DIR", str(line.parent))

    def turn_ends(agent, event, error=None):
        monkeypatch.setenv("AGENT_NAME", agent)
        payload = {"hook_event_name": event, "error": error, "last_assistant_message": "", "session_id": "s"}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setattr("claude_kitchen.channel.send_to_socket", lambda *_: None)
        cmd_hook(argparse.Namespace(command="hook"))

    turn_ends("sous", "StopFailure", "authentication_failed")
    turn_ends("sous", "StopFailure", "rate_limit")  # failed too, so it re-arms nothing
    turn_ends("eng", "StopFailure", "authentication_failed")
    assert fake.sent == [(SOLO, "sous: my Claude login expired; run /login in my pane")]
    turn_ends("sous", "Stop")
    turn_ends("eng", "StopFailure", "authentication_failed")
    assert fake.sent[1:] == [(SOLO, "eng: my Claude login expired; run /login in my pane")]


def test_the_sous_is_told_the_name_on_its_lines_contact_card():
    """People text the agent by its line's persona (the fake's line is Cedar), so the
    plow-chat server's instructions, which reach the sous's system prompt, name it."""
    async def main():
        base_url, server = await FakePlow().serve()
        try:
            async with httpx.AsyncClient(base_url=base_url, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
                return await plow_chat.introduction(client)
        finally:
            server.should_exit = True
    _, options = reply_server("plow-chat", "Plow chat (iMessage)", None, instructions=asyncio.run(main()))
    assert "contact card named Cedar, so Cedar is your name" in options.instructions
