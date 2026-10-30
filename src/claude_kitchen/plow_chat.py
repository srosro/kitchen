"""plow-chat channel server — bridges a Plow chat line to the sous.

Holds the Plow chat API socket for the line's agent credential, pushes each
member's text into the sous as a `<channel source="plow-chat">` tag, and gives
the sous a `reply` tool. No allowlist: Plow shows the credential only the chats
the line's account owns, so every chat and every member it delivers is served.
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

CREDENTIALS = "plow-credentials"  # written mode 600 by `plow-agents mint LINE --credential-file`
RECONNECT_DELAY = 5
PAGE = 50


def log(msg: str):
    print(f"plow-chat: {msg}", file=sys.stderr, flush=True)


def read_credentials(directory: Path) -> dict:
    """`PLOW_API_BASE` and `PLOW_AGENT_TOKEN`, from the env file `plow-agents mint` writes."""
    lines = (directory / CREDENTIALS).read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))


@dataclass
class Bridge:
    client: httpx.AsyncClient
    deliver: callable  # async (content, meta) -> None
    # Chat -> uids of its latest messages taken, persisted so neither the
    # socket/catch-up overlap nor a restart takes one twice. Uids, not a
    # high-water mark: a text sent earlier can land later and sort below one
    # already taken.
    cursor: Path
    taken: dict = field(init=False)

    def __post_init__(self):
        self.taken = json.loads(self.cursor.read_text()) if self.cursor.exists() else {}

    async def take(self, message: dict):
        """Hand a member's text to the sous once; our own sends and Plow's
        system notes are only recorded."""
        uids = self.taken.setdefault(message["chat_uid"], [])
        if message["uid"] in uids:
            return
        if message["sender"]["type"] == "member":
            await self.deliver(message["body"], {"from": message["sender"]["display_name"], "chat": message["chat_uid"]})
        uids.append(message["uid"])  # after delivery, so a crash mid-send can't suppress it
        del uids[:-2 * PAGE]
        atomic_write_json(self.cursor, self.taken)

    async def catch_up(self, chat: str):
        """Take what reached `chat` while the socket was down, from its newest
        page: Plow pages only backwards. A gap of more than one page loses its
        oldest texts. A busy or failing Plow raises into the reconnect retry."""
        r = await self.client.get(f"/v1/chats/{chat}/messages", params={"limit": PAGE})
        if r.status_code == 429 or r.status_code >= 500:
            r.raise_for_status()
        if r.is_error:
            log(f"HTTP {r.status_code} reading chat {chat}; skipped")
            return
        for message in reversed(r.json()["data"]):
            await self.take(message)

    async def reply(self, chat: str, text: str) -> str:
        if not text.strip():
            raise ValueError("reply text is empty; not sent")
        r = await self.client.post(f"/v1/chats/{chat}/messages", json={"body": text})
        if r.is_error:
            raise RuntimeError(f"Plow refused the reply with HTTP {r.status_code}")
        return f"sent {r.json()['uid']}"

    async def run(self):
        """Hold the socket forever: a fresh ticket per connect, catch up every
        known chat once it is open, and take each message frame as it lands."""
        ws_url = str(self.client.base_url).rstrip("/").replace("http", "ws", 1) + "/v1/ws"
        while True:
            try:
                r = await self.client.post("/v1/ws/ticket", json={})
                r.raise_for_status()
                async with websockets.connect(f"{ws_url}?ticket={r.json()['ticket']}") as ws:
                    for chat in list(self.taken):
                        await self.catch_up(chat)
                    log("connected")
                    async for raw in ws:
                        event = json.loads(raw)
                        if event.get("event_type") == "message_received":
                            await self.take(event["data"]["message"])
                log("socket closed")
            except Exception as e:
                # The supervisor is a detached task: it must never die quietly
                # while the sous waits for texts.
                log(f"reconnecting after {type(e).__name__}: {e}")
            await asyncio.sleep(RECONNECT_DELAY)


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
    credentials = read_credentials(directory)
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

    headers = {"Authorization": f"Bearer {credentials['PLOW_AGENT_TOKEN']}"}
    async with httpx.AsyncClient(base_url=credentials["PLOW_API_BASE"], headers=headers, timeout=30) as client, \
            stdio_server() as (read_stream, write_stream):
        bridge = Bridge(client, lambda content, meta: push(write_stream, content, meta), directory / "last_seen.json")
        supervisor = asyncio.create_task(bridge.run())
        try:
            await server.run(read_stream, write_stream, init_options)
        finally:
            supervisor.cancel()


def main(kitchen: str):
    asyncio.run(run_server(kitchen))
