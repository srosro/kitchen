"""Kitchen channel MCP server — bridges cook hooks to sous chef via Claude Code channels."""
import asyncio
import fcntl
import json
import socket as sock_mod
import sys
from pathlib import Path
from typing import get_args, get_origin

from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification, TextContent, Tool

SOCK_NAME = "kitchen.sock"

# One message is one line on the socket, and a line past the reader's limit
# fails inside handle_connection after the sender has already succeeded, so the
# message vanishes. asyncio's 64 KiB default is within reach of a long report.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024

# The capability that makes a sous hear anything. Claude reads it from a
# DIFFERENT place depending on the protocol era it negotiates with us:
# `experimental` on the handshake era (2025-11-25), `extensions` on the modern
# era (2026-07-28, SEP-2133). Declaring only one is not an error anywhere — the
# connection succeeds, the socket binds, cook reports arrive, and Claude
# silently drops every notification, logging "Channel notifications skipped:
# server did not declare claude/channel capability" where nobody looks. So
# declare it in both, and refuse to start if the SDK grows a third place.
CHANNEL_CAPABILITY = "claude/channel"


def _capability_maps(capabilities) -> list[str]:
    """Names of every free-form capability map the installed SDK exposes.

    Both `experimental` and `extensions` are `dict[str, dict[str, Any]]` on
    ServerCapabilities; the typed sub-capabilities (`tools`, `logging`, …) are
    models. An era that moves the capability again will land as another field
    of that same free-form shape, and this finds it without being told.

    Matched structurally, not by the annotation's text: this whole outage was a
    silent formatting assumption, and `str(annotation)` renders `Any` as
    `typing.Any` on some versions and not others."""
    def is_map(annotation) -> bool:
        for arg in get_args(annotation) or (annotation,):
            if get_origin(arg) is dict and get_args(arg)[0] is str:
                return get_origin(get_args(arg)[1]) is dict
        return False

    return [
        name for name, field in type(capabilities).model_fields.items()
        if is_map(field.annotation)
    ]


def _refuse_if_deaf(capabilities):
    """Exit rather than serve a connection that cannot deliver.

    A server that binds the socket but cannot push is worse than no server at
    all: it owns `kitchen.sock`, so cook hooks succeed and the sous hears
    nothing, and the replacement that would have worked stands down because the
    path is taken. Checked BEFORE the socket is claimed, so a deaf build dies
    without taking the kitchen with it."""
    missing = [
        name for name in _capability_maps(capabilities)
        if CHANNEL_CAPABILITY not in (getattr(capabilities, name) or {})
    ]
    if missing:
        sys.exit(
            f"kitchen channel-server: this mcp SDK carries capabilities in "
            f"{', '.join(missing)}, where we do not advertise "
            f"{CHANNEL_CAPABILITY}. A sous negotiating that protocol era would "
            f"connect, bind the socket and never hear a cook. Advertise "
            f"{CHANNEL_CAPABILITY} in {', '.join(missing)} in "
            f"create_initialization_options (channel.py)."
        )


def reply_server(name: str, medium: str, call, *tools: Tool):
    """A chat channel's MCP server and init options: a `reply` tool plus
    `tools`, each run by `call(tool_name, arguments) -> str`, and the channel
    capability."""
    server = Server(name)
    reply = Tool(
        name="reply",
        description=(f"Send a message to a {medium}; pass the `chat` attribute of the "
                     f"<channel source=\"{name}\"> tag you are answering."),
        inputSchema={
            "type": "object",
            "properties": {"chat": {"type": "string"}, "text": {"type": "string"}},
            "required": ["chat", "text"],
        },
    )

    @server.list_tools()
    async def list_tools():
        return [reply, *tools]

    @server.call_tool()
    async def call_tool(tool: str, arguments: dict):
        return [TextContent(type="text", text=await call(tool, arguments))]

    options = server.create_initialization_options(
        notification_options=NotificationOptions(),
        experimental_capabilities={CHANNEL_CAPABILITY: {}},
    )
    _refuse_if_deaf(options.capabilities)
    return server, options


async def push(write_stream, content: str, meta: dict):
    """Send one channel notification; Claude renders it as a <channel> tag
    with the server's name as `source` and each meta key as an attribute."""
    notification = JSONRPCNotification(
        jsonrpc="2.0",
        method=f"notifications/{CHANNEL_CAPABILITY}",
        params={"content": content, "meta": meta},
    )
    await write_stream.send(SessionMessage(message=JSONRPCMessage(notification)))


async def handle_connection(reader: asyncio.StreamReader, writer, notify):
    """Handle one cook hook connection: read JSON line, call notify."""
    try:
        line = await reader.readline()
        if not line:
            return
        data = json.loads(line)
        await notify(data)
    except (json.JSONDecodeError, ConnectionError):
        pass
    finally:
        if writer:
            writer.close()


def _claim_socket(sock_path: Path):
    """Own sock_path for this server's life through a lock beside it, and
    return the lock's file, which must stay open.

    The lock is released only when the process exits, after its cleanup has
    unlinked the socket, so of two servers for one kitchen only the holder
    ever unlinks or binds the path: the other stands down. A path that is not
    a socket is left alone, loudly.
    """
    owner = open(sock_path.with_name(sock_path.name + ".lock"), "a")
    try:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"kitchen channel-server: {sock_path} already has a live owner; standing down.", file=sys.stderr)
        raise SystemExit(0)
    if sock_path.exists() and not sock_path.is_socket():
        print(f"kitchen channel-server: refusing to bind {sock_path}: not a socket; leaving it untouched.",
              file=sys.stderr)
        raise SystemExit(1)
    # A server that predates the lock holds none; a listener answering here is one.
    with sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM) as probe:
        if sock_path.exists() and probe.connect_ex(str(sock_path)) == 0:
            print(f"kitchen channel-server: {sock_path} already has a live owner; standing down.", file=sys.stderr)
            raise SystemExit(0)
    sock_path.unlink(missing_ok=True)
    return owner


async def run_server(kitchen: str):
    """Run the MCP channel server with a unix socket listener."""
    from claude_kitchen.state import state_dir

    base = state_dir(kitchen)
    base.mkdir(parents=True, exist_ok=True)
    sock_path = base / SOCK_NAME

    server = Server("kitchen")
    init_options = server.create_initialization_options(
        notification_options=NotificationOptions(),
        experimental_capabilities={CHANNEL_CAPABILITY: {}},
    )
    # Before the socket is claimed: a server that cannot deliver must not own
    # the path the next one needs.
    _refuse_if_deaf(init_options.capabilities)
    owner = _claim_socket(sock_path)

    # Mutable container so notify closure can access write_stream
    # after stdio_server connects.
    state = {"write_stream": None}

    async def notify(data: dict):
        ws = state["write_stream"]
        if ws is None:
            return
        cook = data.get("cook", "unknown")
        summary = data.get("summary", "")
        ts = data.get("ts", "")
        meta = {"cook": cook, "ts": ts}
        # ctx is omitted (not passed null/empty) when the cook has no
        # token info yet — the sous shouldn't see ctx="" or ctx="null".
        if data.get("ctx"):
            meta["ctx"] = data["ctx"]
        await push(ws, summary, meta)

    async def on_connection(reader, writer):
        await handle_connection(reader, writer, notify)

    # Start socket server FIRST so it's ready before MCP handshake
    sock_server = await asyncio.start_unix_server(
        on_connection, path=str(sock_path), limit=MAX_MESSAGE_BYTES
    )

    try:
        async with stdio_server() as (read_stream, write_stream):
            state["write_stream"] = write_stream
            await server.run(read_stream, write_stream, init_options)
    finally:
        sock_server.close()
        await sock_server.wait_closed()
        sock_path.unlink(missing_ok=True)
        owner.close()  # only now may the next server claim the path


def main(kitchen: str):
    """Entry point for the channel server process."""
    asyncio.run(run_server(kitchen))


def deliver_to_socket(sock_path: Path, data: dict):
    """Send a JSON line to the kitchen socket. OSError means nothing is listening."""
    with sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM) as s:
        s.connect(str(sock_path))
        s.sendall(json.dumps(data).encode() + b"\n")


def send_to_socket(sock_path: Path, data: dict):
    """Send a JSON line to the kitchen socket on behalf of a hook.

    Loud, not fatal: a wedged/hijacked/dead socket is surfaced to stderr (path +
    errno) so the failure is immediately diagnosable, but never crashes the
    calling cook hook — a dead channel must not break the cook.
    """
    try:
        deliver_to_socket(sock_path, data)
    except OSError as e:
        # ConnectionError / FileNotFoundError are OSError subclasses.
        print(
            f"kitchen channel: failed to notify sous via {sock_path}: "
            f"[errno {e.errno}] {e.strerror or e}",
            file=sys.stderr,
        )
