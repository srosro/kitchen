"""plow-chat channel server — bridges a Plow chat line to the sous.

Holds the Plow chat API socket for the line's agent token, pushes each
allowlisted inbound text into the sous as a `<channel source="plow-chat">` tag,
and gives the sous a `reply` tool that posts back to allowlisted chats only.
"""
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
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


def mask(value: str) -> str:
    return "…***" if len(value) < 8 else f"…{value[-3:]}"


def log_dropped(message: dict):
    handle = (message.get("sender") or {}).get("provider_key") or ""
    log(f"dropped {message['uid']}: sender {mask(handle)} in chat {message['chat_uid']} is not allowlisted")


@dataclass(frozen=True)
class Config:
    token: str
    senders: dict[str, str]  # sender provider_key -> name the sous sees
    chats: frozenset[str]
    base_url: str = DEFAULT_BASE_URL


def load_config(directory: Path) -> Config:
    """`config.json` holds the allowlist; the agent token sits beside it in a
    mode-600 `token` file so it never reaches argv, env or the repo."""
    token_file = directory / "token"
    mode = token_file.stat().st_mode & 0o777
    if mode != 0o600:
        sys.exit(f"plow-chat: {token_file} is mode {mode:o}; run chmod 600 {token_file}")
    raw = json.loads((directory / "config.json").read_text())
    return Config(
        token=token_file.read_text().strip(),
        senders=raw["senders"],
        chats=frozenset(raw["chats"]),
        base_url=raw.get("base_url", DEFAULT_BASE_URL),
    )


@dataclass
class Bridge:
    config: Config
    client: httpx.AsyncClient
    deliver: callable  # async (content, meta) -> None
    # Per-chat high-water mark: the created_at of the newest message handled,
    # plus the uids handled at exactly that instant (Plow stamps created_at
    # with the transaction's now(), so two messages can share one). Persisted
    # so a restart replays nothing older and misses nothing newer.
    cursor: Path
    marks: dict = field(init=False)

    def __post_init__(self):
        self.marks = json.loads(self.cursor.read_text()) if self.cursor.exists() else {}

    def _handled(self, chat: str, message: dict) -> bool:
        mark = self.marks[chat]
        if not mark["at"]:
            return False
        at, then = datetime.fromisoformat(message["created_at"]), datetime.fromisoformat(mark["at"])
        return at < then or (at == then and message["uid"] in mark["uids"])

    def _advance(self, chat: str, messages: list[dict]):
        """Move the mark to the newest of `messages`, keeping every uid at that instant."""
        mark = self.marks.get(chat, {"at": "", "uids": []})
        for message in messages:
            if mark["at"] and datetime.fromisoformat(message["created_at"]) == datetime.fromisoformat(mark["at"]):
                mark["uids"].append(message["uid"])
            else:
                mark = {"at": message["created_at"], "uids": [message["uid"]]}
        self.marks[chat] = mark
        atomic_write_json(self.cursor, self.marks)  # mkstemp: mode 600

    async def sync(self, chat: str):
        """Hand the sous every inbound text in `chat` newer than its mark,
        oldest first. The REST page is the record and a socket frame only a
        doorbell, so live, reconnect and restart share this one path and an
        out-of-order frame can't skip a text. A chat with no mark yet is only
        measured, so its history is never replayed. A chat Plow refuses is
        logged and skipped, so one bad uid can't starve the others."""
        r = await self.client.get(f"/v1/chats/{chat}/messages", params={"limit": CATCH_UP_LIMIT})
        if r.is_error:
            log(f"HTTP {r.status_code} reading chat {chat}; skipped")
            return
        messages = r.json()["data"][::-1]  # newest-first → oldest-first
        if chat not in self.marks:
            self._advance(chat, messages)
            return
        for message in messages:
            if self._handled(chat, message):
                continue
            if message["direction"] == "inbound":
                name = self.config.senders.get((message.get("sender") or {}).get("provider_key"))
                if name is None:
                    log_dropped(message)
                else:
                    await self.deliver(message["body"], {"from": name, "chat": chat})
            # Advanced only after delivery, so a crash mid-send can't suppress it.
            self._advance(chat, [message])

    async def on_frame(self, message: dict):
        chat = message["chat_uid"]
        if chat in self.config.chats:
            await self.sync(chat)
        elif message["direction"] == "inbound":
            log_dropped(message)

    async def reply(self, chat: str, text: str) -> str:
        if chat not in self.config.chats:
            raise ValueError(f"chat {chat} is not on the plow-chat allowlist; not sent")
        if not text.strip():
            raise ValueError("reply text is empty; not sent")
        r = await self.client.post(f"/v1/chats/{chat}/messages", json={"body": text})
        if r.is_error:
            raise RuntimeError(f"Plow refused the reply with HTTP {r.status_code}")
        return f"sent {r.json()['uid']}"

    async def run(self):
        """Hold the socket forever: fresh ticket per connect, sync every
        allowed chat after each `connected`, back off exponentially on any drop."""
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
                    for chat in sorted(self.config.chats):
                        await self.sync(chat)
                    backoff = 1
                    log("connected")
                    async for raw in ws:
                        event = json.loads(raw)
                        if event.get("event_type") == "message_received":
                            await self.on_frame(event["data"]["message"])
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
        "Send a text message to a Plow chat (iMessage). Only chats on the "
        "plow-chat allowlist are accepted; pass the `chat` attribute of the "
        "<channel source=\"plow-chat\"> tag you are answering."
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
