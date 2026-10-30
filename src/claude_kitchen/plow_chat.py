"""plow-chat channel server — bridges a Plow chat line to the sous.

Holds the Plow chat API socket for the line's agent token, pushes each member's
text into the sous as a `<channel source="plow-chat">` tag, and gives the sous a
`reply` tool. No allowlist: Plow shows the token only the chats the line's
owning account owns, so every chat and every member it delivers is served.
"""
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import mcp.types as types
import websockets
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server

from claude_kitchen.channel import CHANNEL_CAPABILITY, _refuse_if_deaf, push
from claude_kitchen.state import atomic_write_json, plow_chat_dir

DEFAULT_BASE_URL = "https://api.plow.co"
MAX_BACKOFF = 30
CATCH_UP_LIMIT = 50


def log(msg: str):
    print(f"plow-chat: {msg}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class Config:
    token: str
    base_url: str = DEFAULT_BASE_URL


def load_config(directory: Path) -> Config:
    """The line's agent token sits in a mode-600 `token` file so it never
    reaches argv, env or the repo."""
    token_file = directory / "token"
    mode = token_file.stat().st_mode & 0o777
    if mode != 0o600:
        sys.exit(f"plow-chat: {token_file} is mode {mode:o}; run chmod 600 {token_file}")
    token = token_file.read_text().strip()
    if not token:
        sys.exit(f"plow-chat: {token_file} is empty; write the line's agent token into it")
    return Config(token=token)


@dataclass
class Bridge:
    config: Config
    client: httpx.AsyncClient
    deliver: callable  # async (content, meta) -> None
    # Per chat, the uids of every message on the last page read, persisted so
    # a restart replays nothing and misses nothing that is still on the page.
    # Uids, not timestamps: inbound created_at is the sender's send time,
    # outbound is the server's, so a late inbound can sort below our own echo.
    cursor: Path
    seen: dict = field(init=False)

    def __post_init__(self):
        record = json.loads(self.cursor.read_text()) if self.cursor.exists() else {}
        if not all(isinstance(uids, list) for uids in record.values()):
            sys.exit(f"plow-chat: {self.cursor} is not a list of message uids per chat; delete it to start fresh")
        self.seen = {chat: set(uids) for chat, uids in record.items()}

    def _save(self):
        atomic_write_json(self.cursor, {chat: sorted(uids) for chat, uids in self.seen.items()})  # mkstemp: mode 600

    async def _page(self, chat: str, starting_after: str = None) -> dict | None:
        """One page of `chat`, newest first. A busy or failing Plow (429, 5xx)
        raises into the reconnect loop's backoff; any other refusal (a deleted
        or re-created group) is logged and skipped so it can't starve the others."""
        params = {"limit": CATCH_UP_LIMIT, **({"starting_after": starting_after} if starting_after else {})}
        r = await self.client.get(f"/v1/chats/{chat}/messages", params=params)
        if r.status_code == 429 or r.status_code >= 500:
            r.raise_for_status()
        if r.is_error:
            log(f"HTTP {r.status_code} reading chat {chat}; skipped")
            return None
        return r.json()

    async def sync(self, chat: str, fresh: str = None):
        """Hand the sous every inbound text in `chat` it hasn't seen, oldest
        first, paging back until it reaches messages it has. The REST record is
        the source and a socket frame only a doorbell, so live, reconnect and
        restart share this one path. A chat seen for the first time has its
        history recorded, never replayed: only `fresh`, the text whose frame
        rang, and anything after it are new in it."""
        page = await self._page(chat)
        if page is None:
            return
        newest = page["data"]
        uids = [m["uid"] for m in newest]
        self.seen.setdefault(chat, set(uids[uids.index(fresh) + 1:] if fresh in uids else uids))
        unread = list(newest)
        while not (found := bool(self.seen[chat] & {m["uid"] for m in page["data"]})) and page["has_more"]:
            page = await self._page(chat, page["data"][-1]["uid"])
            if page is None:
                return
            unread += page["data"]
        if self.seen[chat] and not found:
            # Every message we recorded has left the history, so "unseen"
            # would be the whole chat: replaying it to a sous that acts on
            # texts is the one outcome worse than missing some.
            log(f"chat {chat}: none of its recorded messages is in its history any more; "
                f"recording its {len(unread)} messages WITHOUT delivering them")
            unread = []
        for message in reversed(unread):  # newest-first → oldest-first
            if message["uid"] in self.seen[chat]:
                continue
            if message["sender"]["type"] == "member":  # not our own sends, nor Plow's system notes
                await self.deliver(message["body"], {"from": message["sender"]["display_name"], "chat": chat})
            # Recorded only after delivery, so a crash mid-send can't suppress it.
            self.seen[chat].add(message["uid"])
            self._save()
        # The next read stops at the newest page, so it is all the record needs.
        self.seen[chat] = {message["uid"] for message in newest}
        self._save()

    async def reply(self, chat: str, text: str) -> str:
        if not text.strip():
            raise ValueError("reply text is empty; not sent")
        r = await self.client.post(f"/v1/chats/{chat}/messages", json={"body": text})
        if r.is_error:
            raise RuntimeError(f"Plow refused the reply with HTTP {r.status_code}")
        return f"sent {r.json()['uid']}"

    async def run(self):
        """Hold the socket forever: fresh ticket per connect, sync every
        chat already read after each `connected`, back off exponentially on any drop."""
        backoff = 1
        ws_url = self.config.base_url.replace("http", "ws", 1) + "/v1/ws"
        while True:
            try:
                r = await self.client.post("/v1/ws/ticket", json={})
                r.raise_for_status()
                async with websockets.connect(f"{ws_url}?ticket={r.json()['ticket']}") as ws:
                    hello = json.loads(await ws.recv())
                    if hello.get("type") != "connected":
                        raise ConnectionError(f"expected a connected frame, got {hello.get('type')!r}")
                    for chat in sorted(self.seen):
                        await self.sync(chat)
                    backoff = 1
                    log("connected")
                    async for raw in ws:
                        event = json.loads(raw)
                        if event.get("event_type") == "message_received":
                            message = event["data"]["message"]
                            await self.sync(message["chat_uid"], message["uid"])
                log("socket closed")
            except httpx.HTTPStatusError as e:
                log(f"HTTP {e.response.status_code} from {e.request.url.path}")
            except Exception as e:
                # Any failure — a dropped socket or a frame we can't parse —
                # reconnects; the supervisor is a detached task and must
                # never die quietly while the sous waits for texts.
                log(f"reconnecting after {type(e).__name__}: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)


REPLY_TOOL = types.Tool(
    name="reply",
    description=(
        "Send a text message to a Plow chat (iMessage); pass the `chat` "
        "attribute of the <channel source=\"plow-chat\"> tag you are answering."
    ),
    inputSchema={
        "type": "object",
        "properties": {"chat": {"type": "string"}, "text": {"type": "string"}},
        "required": ["chat", "text"],
    },
)


async def run_server(kitchen: str):
    directory = plow_chat_dir(kitchen)
    config = load_config(directory)
    server = Server("plow-chat")
    bridge = None  # bound once the HTTP client and stdio streams are open

    @server.list_tools()
    async def list_tools():
        return [REPLY_TOOL]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        return [types.TextContent(type="text", text=await bridge.reply(arguments["chat"], arguments["text"]))]

    init_options = server.create_initialization_options(
        notification_options=NotificationOptions(),
        experimental_capabilities={CHANNEL_CAPABILITY: {}},
    )
    _refuse_if_deaf(init_options.capabilities)

    headers = {"Authorization": f"Bearer {config.token}"}
    async with httpx.AsyncClient(base_url=config.base_url, headers=headers, timeout=30) as client, \
            stdio_server() as (read_stream, write_stream):
        bridge = Bridge(config, client, lambda content, meta: push(write_stream, content, meta),
                        directory / "last_seen.json")
        supervisor = asyncio.create_task(bridge.run())
        try:
            await server.run(read_stream, write_stream, init_options)
        finally:
            supervisor.cancel()


def main(kitchen: str):
    asyncio.run(run_server(kitchen))
