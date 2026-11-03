"""buzz channel server — bridges a Buzz relay to the sous.

Holds one socket to the relay as the sous's own member key, follows every
channel and DM that key belongs to, pushes each other member's text into the
sous as a `<channel source="buzz">` tag, and gives the sous a `reply` tool.
No allowlist: the relay is closed to everyone its owner hasn't added, and
only channels this key was added to are heard, so every member speaking in
them is served.
"""
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import mcp.types as types
import websockets
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server

from claude_kitchen import nostr
from claude_kitchen.channel import CHANNEL_CAPABILITY, _refuse_if_deaf, push
from claude_kitchen.state import atomic_write_json, read_env, state_dir

CREDENTIALS = "buzz-credentials"  # BUZZ_RELAY_URL and the member key's hex BUZZ_PRIVATE_KEY, mode 600
RECONNECT_DELAY = 5
MESSAGE, PROFILE, MEMBERS = 9, 0, 39002
SEEN = 200  # event ids remembered, so a reconnect's replay of the last second isn't delivered twice


def log(msg: str):
    print(f"buzz: {msg}", file=sys.stderr, flush=True)


def tag_values(event: dict, name: str) -> list[str]:
    return [t[1] for t in event["tags"] if len(t) > 1 and t[0] == name]


@dataclass
class Bridge:
    secret: bytes
    deliver: callable  # async (content, meta) -> None
    # Where to resume and what was already delivered, persisted so neither a
    # reconnect nor a restart delivers a text twice.
    cursor: Path
    me: str = field(init=False)
    state: dict = field(init=False)
    channels: set = field(init=False, default_factory=set)
    members: set = field(init=False, default_factory=set)
    names: dict = field(init=False, default_factory=dict)
    ws: object = None
    pending: dict = field(init=False, default_factory=dict)  # event id -> future of its OK

    def __post_init__(self):
        self.me = nostr.public_key(self.secret).hex()
        self.state = json.loads(self.cursor.read_text()) if self.cursor.exists() else None

    async def send(self, *frame):
        await self.ws.send(json.dumps(list(frame)))

    async def follow(self):
        """(Re)issue the profile and message subscriptions over every channel
        known; message history resumes at the last text delivered, or now on
        the first start."""
        if self.state is None:
            self.state = {"since": int(time.time()), "seen": []}
            atomic_write_json(self.cursor, self.state)
        if self.channels:
            await self.send("REQ", "profiles", {"kinds": [PROFILE], "authors": sorted(self.members)})
            await self.send("REQ", "messages", {"kinds": [MESSAGE], "#h": sorted(self.channels),
                                                "since": self.state["since"]})

    async def handle(self, frame: list):
        if frame[0] == "OK" and frame[1] in self.pending:
            self.pending.pop(frame[1]).set_result(frame)
        if frame[0] != "EVENT":
            return
        event = frame[2]
        if event["kind"] == MEMBERS:
            known = (set(self.channels), set(self.members))
            self.channels.update(tag_values(event, "d"))
            self.members.update(tag_values(event, "p"))
            if (self.channels, self.members) != known:
                await self.follow()
        elif event["kind"] == PROFILE:
            profile = json.loads(event["content"] or "{}")
            self.names[event["pubkey"]] = profile.get("display_name") or profile.get("name")
        elif event["kind"] == MESSAGE:
            await self.take(event)

    async def take(self, event: dict):
        """Hand another member's text to the sous once; our own sends and
        text-less messages are only recorded."""
        if event["id"] in self.state["seen"]:
            return
        if event["pubkey"] != self.me and event["content"].strip():
            name = self.names.get(event["pubkey"]) or event["pubkey"][:8]
            await self.deliver(event["content"], {"from": name, "chat": tag_values(event, "h")[0]})
        self.state["seen"] = (self.state["seen"] + [event["id"]])[-SEEN:]
        self.state["since"] = max(self.state["since"], event["created_at"])
        atomic_write_json(self.cursor, self.state)

    async def reply(self, chat: str, text: str) -> str:
        if not text.strip():
            raise ValueError("reply text is empty; not sent")
        if chat not in self.channels:
            raise ValueError(f"not a member of {chat}; not sent")
        event = nostr.event(self.secret, MESSAGE, text, [["h", chat]])
        ok = asyncio.get_running_loop().create_future()
        self.pending[event["id"]] = ok
        try:
            await self.send("EVENT", event)
            _, _, accepted, message = await asyncio.wait_for(ok, 15)
        finally:
            self.pending.pop(event["id"], None)  # so an OK arriving after the timeout finds nothing to resolve
        if not accepted:
            raise RuntimeError(f"relay refused the reply: {message}")
        return f"sent {event['id']}"

    async def run(self, url: str):
        """Hold the socket forever: authenticate on the relay's challenge,
        follow this key's memberships, and handle each frame as it lands."""
        while True:
            try:
                async with websockets.connect(url) as ws:
                    self.ws = ws
                    challenge = json.loads(await ws.recv())
                    auth = nostr.event(self.secret, 22242, "", [["relay", url], ["challenge", challenge[1]]])
                    await self.send("AUTH", auth)
                    if not (ok := json.loads(await ws.recv()))[2]:
                        raise RuntimeError(f"relay refused this key: {ok[3]}")
                    await self.send("REQ", "members", {"kinds": [MEMBERS], "#p": [self.me]})
                    await self.follow()
                    log("connected")
                    async for raw in ws:
                        await self.handle(json.loads(raw))
            except Exception as e:
                # The supervisor is a detached task: it must never die quietly
                # while the sous waits for texts.
                log(f"reconnecting after {type(e).__name__}: {e}")
            await asyncio.sleep(RECONNECT_DELAY)


REPLY_TOOL = types.Tool(
    name="reply",
    description=(
        "Send a message to a Buzz channel or DM; pass the `chat` attribute of "
        "the <channel source=\"buzz\"> tag you are answering."
    ),
    inputSchema={
        "type": "object",
        "properties": {"chat": {"type": "string"}, "text": {"type": "string"}},
        "required": ["chat", "text"],
    },
)


async def run_server(kitchen: str):
    directory = state_dir(kitchen) / "buzz"
    credentials = read_env(directory / CREDENTIALS)
    server = Server("buzz")
    bridge = None  # bound once the stdio streams are open

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

    async with stdio_server() as (read_stream, write_stream):
        bridge = Bridge(bytes.fromhex(credentials["BUZZ_PRIVATE_KEY"]),
                        lambda content, meta: push(write_stream, content, meta), directory / "last_seen.json")
        supervisor = asyncio.create_task(bridge.run(credentials["BUZZ_RELAY_URL"]))
        try:
            await server.run(read_stream, write_stream, init_options)
        finally:
            supervisor.cancel()


def main(kitchen: str):
    asyncio.run(run_server(kitchen))
