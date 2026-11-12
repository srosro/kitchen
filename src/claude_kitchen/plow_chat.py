"""plow-chat channel server — bridges a Plow chat line to the sous.

Holds the Plow chat API socket for the line's agent credential, pushes each
member's text and attachments into the sous as a `<channel source="plow-chat">` tag, and gives
the sous `reply`, `send_file` and `start_chat` tools. Plow shows the credential only the chats the line's
account owns, so every participant it delivers — a person or a peer agent's line — is served.
Once `trusted-chats` exists, only the chats it lists carry authority: any other chat's texts
arrive marked `trusted="false"`, as context the sous may answer but never act on.
`start_chat` reaches only people already in one of the line's chats or listed in
`start-chat-allow`, and opens at most `NEW_CHATS_PER_DAY` new chats a day, each logged.
Every send that reaches anyone but the line's owner first goes past a fresh reviewer
model, which reads the live thread (and, once `messages-host` names the latch host
holding the owner's Messages, the owner's other recent threads with the same people)
and holds a text that contradicts them, makes the owner look out of the loop, or isn't needed.
"""
import asyncio
import json
import mimetypes
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import takewhile
from pathlib import Path

import httpx
import websockets
from mcp.server.stdio import stdio_server
from mcp.types import Tool

from claude_kitchen.channel import push, reply_server
from claude_kitchen.state import atomic_write_json, plow_chat_dir, read_env, state_dir, with_digest

CREDENTIALS = "plow-credentials"  # written mode 600 by `latch agents new --own --credential-file`
TRUSTED_CHATS = "trusted-chats"  # one chat uid per line; read at each hand-over, so edits need no restart
START_CHAT_ALLOW = "start-chat-allow"  # one handle per line, beyond those already in the line's chats
STARTED_CHATS = "started-chats.log"  # one line per chat start_chat sends: time, chat, created, handles
MESSAGES_HOST = "messages-host"  # the latch host holding the owner's Messages, read by the pre-send review
NEW_CHATS_PER_DAY = 5
REVIEW_DAYS = 3  # how far back the review reads the owner's other threads
REVIEW_MODEL = "sonnet"
REVIEW = """An AI agent working for {owner} is about to send the iMessage below to people other than \
{owner}: {people}. You are the second pair of eyes. Someone who gets a wrong, redundant or \
out-of-step text from {owner}'s agent concludes that {owner} is disorganized.

Hold it if any of these is true:
- It contradicts, or has been overtaken by, anything said in this thread or in {owner}'s other \
recent threads with them. The newest texts count most.
- It would make {owner} look out of the loop, or show agents talking past each other or past {owner}.
- It repeats what someone already told them, or answers something nobody asked.
- It isn't needed: {owner} is handling it, or saying nothing is better. When in doubt, hold.

This thread, oldest first ("you" is the agent):
{thread}

{owner}'s other threads with them over the last {days} days, oldest first:
{other}

The draft:
{text}

Answer on one line: SEND, or HOLD: followed by one sentence saying why."""
RECONNECT_DELAY = 5
PAGE = 50
# One speaker's back-to-back texts are one tag. iMessage sends a link as its
# own message only after the sender's phone fetches the preview, so the link
# lands well after the text before it. In 90 days of Plow inbound, 3 s caught
# 72% of links (6 s: 95%; 1.5 s: 35%), traded for less delay per text. The
# window restarts on each text; another speaker or chat ends it.
BURST_SECONDS = 3.0
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
MAX_ATTACHMENTS = 4  # per message
UPLOAD_TIMEOUT = httpx.Timeout(30, read=120)
# No system files, so a guess doesn't change from one machine to another.
STANDARD_TYPES = mimetypes.MimeTypes(filenames=())
# Plow's spelling of the types the standard library misses or names differently.
CONTENT_TYPES = {
    ".3gp": "video/3gpp",
    ".amr": "audio/amr",
    ".avi": "video/x-msvideo",
    ".caf": "audio/x-caf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".epub": "application/epub+zip",
    ".gz": "application/x-gzip",
    ".ico": "image/x-icon",
    ".ics": "text/calendar",
    ".key": "application/x-iwork-keynote-sffkey",
    ".m4a": "audio/x-m4a",
    ".m4v": "video/x-m4v",
    ".mid": "audio/midi",
    ".midi": "audio/midi",
    ".numbers": "application/x-iwork-numbers-sffnumbers",
    ".pages": "application/x-iwork-pages-sffpages",
    ".pkpass": "application/vnd.apple.pkpass",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".rtf": "text/rtf",
    ".vcf": "text/vcard",
    ".wav": "audio/x-wav",
    ".webp": "image/webp",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def log(msg: str):
    print(f"plow-chat: {msg}", file=sys.stderr, flush=True)


def speaker(message: dict) -> tuple:
    return message["chat_uid"], json.dumps(message["sender"], sort_keys=True)


def sender_name(sender: dict) -> str | None:
    """A participant's name: a person's, or a peer agent's line; None for this
    line's own sends and Plow's system notes."""
    if sender["type"] == "member":
        return sender["display_name"]
    if sender["type"] == "agent" and sender["relationship"] == "peer":
        return sender["line"].get("display_name") or sender["line"]["provider_key"]
    return None


def listed(file: Path) -> set[str]:
    """The entries of a one-per-line kitchen file, `#` comments and blanks dropped."""
    return {line.split("#")[0].strip() for line in file.read_text().splitlines()} - {""}


def untrusted(directory: Path, chat: str) -> bool:
    """Whether `chat` lacks authority: true once `trusted-chats` exists and doesn't list it."""
    file = directory / TRUSTED_CHATS
    return file.exists() and chat not in listed(file)


def new_chats_today(log_file: Path) -> int:
    if not log_file.exists():
        return 0
    today = date.today().isoformat()
    return sum(1 for line in log_file.read_text().splitlines()
               if line.startswith(today) and line.split()[2] == "created")


def content_type_of(file: Path) -> str:
    """The type to declare for `file`; Plow decides whether it accepts it."""
    content_type = CONTENT_TYPES.get(file.suffix.lower()) or STANDARD_TYPES.guess_type(file.name)[0]
    if content_type is None:
        raise ValueError(f"can't tell the file type of {file.name} from its extension; not sent")
    return content_type


def attachable(path: str) -> Path:
    """The file at `path`, once it passes every check that needs no Plow."""
    file = Path(path).expanduser()
    if not file.is_file():
        raise ValueError(f"{file} is not a file; not sent")
    size = file.stat().st_size
    if not 0 < size <= MAX_ATTACHMENT_BYTES:
        raise ValueError(f"{file.name} is {size} bytes; Plow takes 1 byte up to 100 MiB; not sent")
    content_type_of(file)
    return file


async def run(*args: str, stdin: str = "") -> str:
    """The stdout of a command, failing loudly with its tail if it fails."""
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate(stdin.encode())
    if proc.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed with exit {proc.returncode}: "
                           f"{(err or out).decode()[-300:]}; not sent")
    return out.decode()


async def ask_reviewer(prompt: str) -> str:
    """A fresh model's answer to `prompt`, with no tools, settings or memory of its own."""
    return await run("claude", "-p", "--safe-mode", "--tools", "", "--no-session-persistence",
                     "--model", REVIEW_MODEL, stdin=prompt)


async def owner_threads(host: str, handles: set[str], owner: str) -> str:
    """The owner's own Messages threads with any of `handles` from the last
    REVIEW_DAYS, read through latch, as `name: text` lines."""
    cutoff = datetime.now().astimezone() - timedelta(days=REVIEW_DAYS)
    chats = json.loads(await run("latch", "messages", "chats", "--host", host, "--limit", "200"))["chats"]
    threads = []
    for chat in chats:
        if (datetime.fromisoformat(chat["last_message_at"]) < cutoff
                or not handles & {p["handle"].casefold() for p in chat["participants"]}):
            continue
        thread = json.loads(await run("latch", "messages", "thread", "--host", host,
                                      "--chat-id", str(chat["id"]), "--limit", "50"))
        lines = [f"{owner if m['from']['me'] else m['from']['name'] or m['from']['handle']}: {m['text']}"
                 for m in thread["messages"] if m["text"] and datetime.fromisoformat(m["date"]) >= cutoff]
        names = ", ".join(p["name"] or p["handle"] for p in chat["participants"])
        threads.append(f"[{owner}, {names}]\n" + "\n".join(lines))
    return "\n\n".join(threads) or "(none)"


def sent(r: httpx.Response, what: str) -> str:
    """Report a message send, failing unless Plow will deliver it. A held send
    answers 200 and is never delivered."""
    if r.is_error:
        raise RuntimeError(f"Plow refused the {what} with HTTP {r.status_code}: {r.text}")
    message = r.json()
    if message.get("held"):
        raise RuntimeError(f"Plow held the {what} and will never deliver it: {message['reason']}")
    return f"sent {message['uid']}"



@dataclass
class Bridge:
    client: httpx.AsyncClient
    deliver: callable  # async (content, meta) -> None
    # Chat -> uids of its latest messages taken, persisted so neither the
    # socket/catch-up overlap nor a restart takes one twice. Uids, not a
    # high-water mark: a text sent earlier can land later and sort below one
    # already taken.
    cursor: Path
    reviewer: callable = ask_reviewer  # async (prompt) -> answer, the pre-send review's model
    taken: dict = field(init=False)
    burst: list = field(init=False, default_factory=list)  # one speaker's texts not yet handed over
    due: float = 0.0  # loop time the burst closes at
    handing: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)  # the socket and a send both take and flush
    starting: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)  # parallel start_chats share the daily cap

    def __post_init__(self):
        self.taken = json.loads(self.cursor.read_text()) if self.cursor.exists() else {}

    async def take(self, message: dict):
        """Add a message to the burst once, handing the burst over first if
        the message is someone else's or in another chat."""
        async with self.handing:
            uids = self.taken.setdefault(message["chat_uid"], [])
            if message["uid"] in uids or message["uid"] in [m["uid"] for m in self.burst]:
                return
            if self.burst and speaker(self.burst[0]) != speaker(message):
                await self._flush()
            self.burst.append(message)
            self.due = asyncio.get_running_loop().time() + BURST_SECONDS

    async def fetch(self, attachment: dict) -> Path:
        """Save an inbound attachment under files/, named by its uid. The Plow
        token goes only to Plow's own API origin: a URL anywhere else gets a
        client of its own, with no credentials."""
        base = self.client.base_url
        url = base.join(attachment["url"])
        if (url.scheme, url.host, url.port) == (base.scheme, base.host, base.port):
            r = await self.client.get(url)
        else:
            async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as other:
                r = await other.get(url)
        r.raise_for_status()
        file = self.cursor.parent / "files" / (attachment["uid"] + Path(attachment["filename"] or "").suffix)
        file.parent.mkdir(mode=0o700, exist_ok=True)
        await asyncio.to_thread(file.write_bytes, r.content)
        return file

    async def flush(self):
        """Hand a participant's burst to the sous as one text, its attachments
        saved locally and their paths in `file`; our own sends, Plow's system
        notes and messages with neither text nor files (tapbacks) are only
        recorded. An attachment with no URL yet, or one that can't be fetched,
        is named in the text rather than holding up the burst."""
        async with self.handing:
            if self.burst:
                await self._flush()

    async def _flush(self):
        first = self.burst[0]
        if sender := sender_name(first["sender"]):
            texts = [m["body"] for m in self.burst if m["body"].strip()]
            files = []
            for attachment in (a for m in self.burst for a in m["attachments"]):
                name = attachment["filename"] or attachment["uid"]
                if not attachment["url"]:
                    texts.append(f"(attachment {name} isn't available)")
                    continue
                try:
                    files.append(str(await self.fetch(attachment)))
                except httpx.HTTPError as e:
                    texts.append(f"(attachment {name} couldn't be fetched: {type(e).__name__})")
            if texts or files:
                meta = {"from": sender, "chat": first["chat_uid"]} | ({"file": " ".join(files)} if files else {})
                if untrusted(self.cursor.parent, first["chat_uid"]):
                    meta["trusted"] = "false"
                await self.deliver("\n\n".join(texts), meta)
        uids = self.taken[first["chat_uid"]]
        uids += [m["uid"] for m in self.burst]  # after delivery, so a crash mid-send can't suppress it
        del uids[:-2 * PAGE]
        atomic_write_json(self.cursor, self.taken)
        self.burst = []

    async def catch_up(self, chat: str):
        """Take what reached `chat` while the socket was down: the newest
        page's messages above the newest one taken. Plow pages only backwards,
        so a gap of more than one page loses its oldest texts; what sits below a
        taken text is history, never replayed; a text still in the burst counts
        as taken. A busy or failing Plow raises into the reconnect retry.
        Returns the messages it took."""
        r = await self.client.get(f"/v1/chats/{chat}/messages", params={"limit": PAGE})
        if r.status_code == 429 or r.status_code >= 500:
            r.raise_for_status()
        if r.is_error:
            log(f"HTTP {r.status_code} reading chat {chat}; skipped")
            return []
        uids = self.taken[chat] + [m["uid"] for m in self.burst]
        newer = list(reversed(list(takewhile(lambda m: m["uid"] not in uids, r.json()["data"]))))
        for message in newer:
            await self.take(message)
        return newer

    async def unread(self, chat: str) -> str | None:
        """Refuse a send while others have texted `chat` since the sous last
        read it: hand those texts over now and say so, so it answers them in
        one message instead of talking past them."""
        if chat not in self.taken:  # never read, so nothing in it is news
            return None
        held = [m for m in self.burst if m["chat_uid"] == chat]  # catch_up may hand these over
        newer = await self.catch_up(chat)
        async with self.handing:  # and the socket may take more while it reads
            held += [m for m in self.burst if m["chat_uid"] == chat]
        news = [m for m in {m["uid"]: m for m in held + newer}.values()
                if sender_name(m["sender"]) and (m["body"].strip() or m["attachments"])]
        if not news:
            return None
        await self.flush()
        return f"not sent: {len(news)} newer message(s) in this chat reach you next; read them, then answer again"

    async def held(self, text: str, chat: str | None = None, handles: list[str] = ()) -> str | None:
        """Why a reviewer holds `text`, bound for `chat` or a new chat with
        `handles`, or None to send it. Only a text the owner alone would read
        goes unreviewed. The thread is read fresh, so a text the sous hasn't
        been shown yet still counts."""
        r = await self.client.get("/v1/chats")
        r.raise_for_status()
        chats = r.json()["data"]
        members = [p for c in chats for p in c["participants"] if p["type"] == "member"]
        owner = next((p["display_name"] for p in members if p["role"] == "owner"), "the head chef")
        people = {h: h for h in handles} | {p["provider_key"]: p["display_name"] for c in chats if c["uid"] == chat
                                             for p in c["participants"] if p["type"] == "member" and p["role"] != "owner"}
        if not people:
            return None
        thread = []
        if chat:
            r = await self.client.get(f"/v1/chats/{chat}/messages", params={"limit": PAGE})
            r.raise_for_status()
            for m in reversed(r.json()["data"]):
                who = "you" if m["sender"]["type"] == "agent" and m["sender"]["relationship"] == "self" else sender_name(m["sender"])
                if who and m["body"].strip():
                    thread.append(f"{who}: {m['body']}")
        host = self.cursor.parent / MESSAGES_HOST
        other = (await owner_threads(listed(host).pop(), {h.casefold() for h in people}, owner)
                 if host.exists() else "(not available)")
        answer = (await self.reviewer(REVIEW.format(
            owner=owner, people=", ".join(people.values()), thread="\n".join(thread) or "(new thread)",
            other=other, days=REVIEW_DAYS, text=text))).strip()
        verdict, _, why = (answer.splitlines()[0] if answer else "").partition(":")
        if verdict.strip("*. ") == "SEND":
            return None
        if verdict.strip("*. ") == "HOLD":
            return f"not sent: a reviewer held it ({why.strip()}). Stay quiet, or bring the head chef the draft and the reason"
        raise RuntimeError(f"the pre-send reviewer answered neither SEND nor HOLD: {answer[:200]!r}; not sent")

    async def reply(self, chat: str, text: str) -> str:
        if not text.strip():
            raise ValueError("reply text is empty; not sent")
        if refusal := await self.held(text, chat) or await self.unread(chat):  # the review is slow; read last
            return refusal
        return sent(await self.client.post(f"/v1/chats/{chat}/messages", json={"body": text}), "reply")

    async def upload(self, chat: str, file: Path) -> str:
        """Declare `file` on `chat` and upload its bytes; returns its attachment uid."""
        data = await asyncio.to_thread(file.read_bytes)
        r = await self.client.post(f"/v1/chats/{chat}/attachments", json={
            "filename": file.name, "content_type": content_type_of(file), "size_bytes": len(data),
        })
        if r.is_error:
            raise RuntimeError(f"Plow refused {file.name} with HTTP {r.status_code}: {r.text}; nothing sent")
        attachment = r.json()
        # Its own client: the signed URL is the provider's, and the shared client
        # would hand it the Plow token.
        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as provider:
            r = await provider.put(attachment["upload_url"], content=data, headers=attachment["upload_headers"])
        # By status alone: an httpx error would print the signed URL, and Plow
        # sends without checking the upload landed.
        if not r.is_success:
            raise RuntimeError(f"uploading {file.name} failed with HTTP {r.status_code}; nothing sent")
        return attachment["uid"]

    async def send_file(self, chat: str, paths: list[str], caption: str = "") -> str:
        """Send local files on `chat` as one message, or nothing if any fails."""
        if not 1 <= len(paths) <= MAX_ATTACHMENTS:
            raise ValueError(f"{len(paths)} files given; a message takes 1 to {MAX_ATTACHMENTS}; nothing sent")
        files = [attachable(path) for path in paths]  # all of them before Plow hears of any
        uids = [await self.upload(chat, file) for file in files]
        if refusal := await self.held(caption or f"(files: {', '.join(file.name for file in files)})", chat) \
                or await self.unread(chat):  # after the uploads and the review, which can take a while
            return refusal
        return sent(await self.client.post(f"/v1/chats/{chat}/messages", json={
            "attachment_uids": uids, "body": caption,
        }), "file")

    async def start_chat(self, handles: list[str], text: str) -> str:
        """Start a chat on this line with `handles` by sending `text`; Plow seats
        the line's owner too, and adopts the thread if those people already have one."""
        async with self.starting:
            return await self._start_chat(handles, text.strip())

    async def _start_chat(self, handles: list[str], text: str) -> str:
        if not text:
            raise ValueError("first message is empty; not sent")
        directory = self.cursor.parent
        r = await self.client.get("/v1/chats")
        r.raise_for_status()
        known = {p["provider_key"].casefold() for c in r.json()["data"] for p in c["participants"] if p["type"] == "member"}
        if (allow := directory / START_CHAT_ALLOW).exists():
            known |= {handle.casefold() for handle in listed(allow)}
        if strangers := [h for h in handles if h.casefold() not in known]:
            raise ValueError(f"{', '.join(strangers)} not in any of this line's chats or {START_CHAT_ALLOW}; not sent")
        log_file = directory / STARTED_CHATS
        if new_chats_today(log_file) >= NEW_CHATS_PER_DAY:
            raise ValueError(f"already started {NEW_CHATS_PER_DAY} new chats today; not sent")
        if refusal := await self.held(text, handles=handles):
            return refusal
        r = await self.client.get("/v1/agents/me")
        r.raise_for_status()
        r = await self.client.post("/v1/chats", json={
            "line_uid": r.json()["line"]["uid"], "members": handles, "body": text,
            "idempotency_key": str(uuid.uuid4()),
        })
        if r.is_error:
            raise RuntimeError(f"Plow refused the new chat with HTTP {r.status_code}: {r.text}")
        chat = r.json()
        with log_file.open("a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {chat['uid']} "
                    f"{'created' if chat['created'] else 'adopted'} {' '.join(handles)}\n")
        async with self.handing:  # a reconnect catches up only known chats; ours and older is history
            if chat["created"]:  # no history, so all a reconnect can find in it is an answer
                self.taken.setdefault(chat["uid"], [])
                atomic_write_json(self.cursor, self.taken)
            elif chat["uid"] not in self.taken:
                r = await self.client.get(f"/v1/chats/{chat['uid']}/messages", params={"limit": PAGE})
                r.raise_for_status()
                data = r.json()["data"]  # newest first; an answer may already sit above ours
                ours = next(i for i, m in enumerate(data)
                            if m["sender"]["type"] == "agent" and m["sender"]["relationship"] == "self"
                            and m["body"] == text)
                self.taken[chat["uid"]] = [m["uid"] for m in data[ours:]]
                atomic_write_json(self.cursor, self.taken)
        await self.catch_up(chat["uid"])  # an answer above ours that a reconnect's catch-up missed
        return f"started {chat['uid']}" if chat["created"] else f"sent in existing chat {chat['uid']}"

    async def run(self):
        """Hold the socket forever: a fresh ticket per connect, catch up every
        known chat once it is open, take each message frame as it lands, and
        hand the burst over once it goes quiet."""
        ws_url = str(self.client.base_url).rstrip("/").replace("http", "ws", 1) + "/v1/ws"
        while True:
            try:
                r = await self.client.post("/v1/ws/ticket", json={})
                r.raise_for_status()
                async with websockets.connect(f"{ws_url}?ticket={r.json()['ticket']}") as ws:
                    for chat in list(self.taken):
                        await self.catch_up(chat)
                    log("connected")
                    while True:
                        quiet = max(0, self.due - asyncio.get_running_loop().time()) if self.burst else None
                        try:
                            raw = await asyncio.wait_for(ws.recv(), quiet)
                        except TimeoutError:
                            await self.flush()
                            continue
                        event = json.loads(raw)
                        if event.get("event_type") == "message_received":
                            await self.take(event["data"]["message"])
            except Exception as e:
                # The supervisor is a detached task: it must never die quietly
                # while the sous waits for texts.
                log(f"reconnecting after {type(e).__name__}: {e}")
            await asyncio.sleep(RECONNECT_DELAY)


SEND_FILE = Tool(
    name="send_file",
    description=(
        "Send 1 to 4 local files (images, PDFs, video, audio or documents, up to 100 MiB "
        "each) to a Plow chat (iMessage) as one message, with an optional caption; pass "
        "the `chat` attribute of the <channel source=\"plow-chat\"> tag you are answering. "
        "If any file fails, nothing is sent."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "chat": {"type": "string"},
            "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAX_ATTACHMENTS},
            "caption": {"type": "string"},
        },
        "required": ["chat", "paths"],
    },
)


START_CHAT = Tool(
    name="start_chat",
    description=(
        "Start a new Plow chat (iMessage) by sending its first message to 1 or more people, "
        "named by E.164 phone number or iMessage email; the head chef is added to it too. "
        "Only people already in one of this line's chats, or allowlisted by the head chef, "
        f"can be reached, and at most {NEW_CHATS_PER_DAY} new chats a day. "
        "Returns the new chat's id, the `chat` to pass to `reply` and `send_file` afterwards."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "handles": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "text": {"type": "string"},
        },
        "required": ["handles", "text"],
    },
)


async def call(bridge: Bridge, name: str, arguments: dict) -> str:
    """Run the Bridge method a tool names, with its arguments as keywords."""
    if name not in ("reply", SEND_FILE.name, START_CHAT.name):
        raise ValueError(f"unknown tool {name}")
    return await getattr(bridge, name)(**arguments)


async def run_server(kitchen: str):
    directory = plow_chat_dir(kitchen)
    credentials = read_env(directory / CREDENTIALS)  # PLOW_API_BASE, PLOW_AGENT_TOKEN
    bridge = None  # bound once the HTTP client and stdio streams are open
    server, init_options = reply_server("plow-chat", "Plow chat (iMessage)",
                                        lambda name, arguments: call(bridge, name, arguments), SEND_FILE, START_CHAT)

    headers = {"Authorization": f"Bearer {credentials['PLOW_AGENT_TOKEN']}"}
    async with httpx.AsyncClient(base_url=credentials["PLOW_API_BASE"], headers=headers, timeout=30) as client, \
            stdio_server() as (read_stream, write_stream):
        bridge = Bridge(client, lambda content, meta: push(write_stream, with_digest(state_dir(kitchen), content), meta), directory / "last_seen.json")
        supervisor = asyncio.create_task(bridge.run())
        try:
            await server.run(read_stream, write_stream, init_options)
        finally:
            supervisor.cancel()


def main(kitchen: str):
    asyncio.run(run_server(kitchen))
