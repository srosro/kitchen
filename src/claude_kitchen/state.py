"""State management for claude-kitchen."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from urllib.request import Request, urlopen

WINDOWS = os.name == "nt"


def lock(f, wait: bool = True):
    """Hold an exclusive lock on the open file `f` until it is closed; when not
    waiting, a lock held elsewhere raises BlockingIOError. flock on Unix; on
    Windows msvcrt's lock on byte 0 (ponytail: polled every 50 ms, no fairness)."""
    if not WINDOWS:
        import fcntl
        fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        return
    import msvcrt
    f.seek(0)
    while True:
        try:
            return msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            if not wait:
                raise BlockingIOError(f"{f.name} is locked")
            time.sleep(0.05)


def pid_alive(pid: int) -> bool:
    """Whether process `pid` runs. On Windows os.kill(pid, 0) would end it, so
    there the process is opened and its exit code read (259 = still active)."""
    if not WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    try:
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel32.CloseHandle(handle)


# Per-kitchen MCP config filename. Deliberately NOT ".mcp.json": Claude Code
# auto-discovers any file named ".mcp.json" by walking up from a cook's cwd,
# which made cooks spawn their own channel-server. This name is handed to the
# sous explicitly via --mcp-config and is never auto-discovered.
MCP_CONFIG_NAME = "kitchen-mcp.json"

# Legacy filename: kitchens opened before the rename wrote the per-kitchen
# config as a discoverable ".mcp.json". We self-heal these on open/resume and
# remove them on close so existing kitchens stop leaking a cook-discoverable
# config.
LEGACY_MCP_CONFIG_NAME = ".mcp.json"

# discord@claude-plugins-official auto-updates; the sous, which bypasses
# permissions, runs only the reviewed 0.0.4.
DISCORD_PLUGIN = Path.home() / ".claude/plugins/marketplaces/claude-plugins-official/external_plugins/discord"
DISCORD_SHA256 = {
    "server.ts": "6edc17d9e9d04930967361ba51c8e03b8f8508647a1dfc11d79ee6a0eb1010b7",
    "bun.lock": "c3d69584e51e46a758bd9c8a8bfef12b46fe9f2bb57a367f4deb073edd26c55e",
    "package.json": "fa2ece9108aca2ae17e7b1909f468ee8f055e53a45882eb25108a3e394f17e46",
}


def state_root() -> Path:
    """Root of all kitchen state — one subdirectory per kitchen. This is the
    kitchen registry now that there's no shared tmux server to enumerate."""
    return Path.home() / ".claude-kitchen"


def state_dir(kitchen: str) -> Path:
    return state_root() / kitchen


def wiki_dir(slug: str) -> Path:
    """A project's wiki: the `<slug> <path>` row for it in the operator's
    ~/.config/kitchen/wikis, else ~/.claude-kitchen/projects/<slug>/wiki."""
    rows = Path.home() / ".config" / "kitchen" / "wikis"
    if rows.exists():
        for row in rows.read_text().splitlines():
            if row.split()[:1] == [slug]:
                return Path(row.split(maxsplit=1)[1]).expanduser()
    return state_root() / "projects" / slug / "wiki"


# A unix socket path must fit sun_path: 104 bytes on macOS, NUL included.
SOCKET_PATH_MAX = 103


def channel_socket(base: Path) -> Path:
    """The kitchen's channel socket: `<base>/kitchen.sock`, or, when that path
    is too long to bind, one under `<state root>/sockets/` named by a hash of
    the kitchen's name. Every binder and sender goes through here."""
    path = base / "kitchen.sock"
    if len(str(path).encode()) <= SOCKET_PATH_MAX:
        return path
    return state_root() / "sockets" / f"{hashlib.sha1(base.name.encode()).hexdigest()[:16]}.sock"


def notes_dir(kitchen: str) -> Path:
    return state_dir(kitchen) / "notes"


def plow_chat_dir(kitchen: str) -> Path:
    return state_dir(kitchen) / "plow-chat"


def read_env(path: Path) -> dict:
    """The KEY=value lines of a credential file."""
    lines = path.read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))


def swarm_binary() -> str:
    """Absolute, as swarm writes its own entry: Claude Code may launch it
    without this PATH, and then reports only "not found in $PATH: stdio"."""
    return os.path.abspath(shutil.which("swarm") or sys.exit("there is no `swarm` on PATH."))


def swarm_links(kitchen: str) -> list[Path]:
    """The links to the swarm accounts this kitchen acts as:
    `<state>/swarm/credential.json` (its own account) and `credential-*.json`
    (one more per other server), each to swarm's copy under ~/.swarm/accounts. On
    Windows, where a symlink needs admin rights, `<state>/swarm` is instead a
    junction to the one account's folder."""
    swarm = state_dir(kitchen) / "swarm"
    links = [swarm / "credential.json", *sorted(swarm.glob("credential-*.json"))]
    linked = lambda link: link.is_symlink() or swarm.is_junction()
    for link in links:
        if link.exists() and not linked(link):
            sys.exit(f"{link} is a pre-accounts swarm credential: move its account into "
                     "~/.swarm/accounts/<server>/<name>/ and link it back here (see the README).")
    return links if linked(links[0]) else []


def swarm_accounts(kitchen: str) -> list[dict]:
    """The credentials of the swarm accounts this kitchen acts as."""
    return [json.loads(link.read_text()) for link in swarm_links(kitchen)]  # a dangling link fails here, loudly


def swarm_account(kitchen: str) -> dict | None:
    """The kitchen's own swarm account, the one its public persona speaks for."""
    return next(iter(swarm_accounts(kitchen)), None)


def mcp_servers(kitchen: str) -> dict:
    """What a sous loads: its channels, plus swarm once this kitchen has an
    account, which `swarm mcp claude` serves itself through Claude Code's inbox."""
    servers = channel_servers(kitchen)
    # One server per account, bound to it: swarm then receives the account's
    # saved rooms again in whatever session it starts in. The kitchen's own
    # account keeps the plain name, so its tools stay `mcp__swarm__*`.
    for i, account in enumerate(swarm_accounts(kitchen)):
        name = "swarm" if i == 0 else "swarm-" + re.sub(r"[^A-Za-z0-9_-]", "-", account["user_id"].lstrip("@"))
        servers[name] = {"command": swarm_binary(), "args": ["mcp", "claude", "--account", account["user_id"]]}
    return servers


def matrix_get(account: dict, path: str) -> dict:
    """A Matrix client-server GET as `account`."""
    url = f"{account['homeserver'].rstrip('/')}/_matrix/client/v3{path}"
    with urlopen(Request(url, headers={"Authorization": f"Bearer {account['access_token']}"}), timeout=30) as r:
        return json.load(r)


# A room's name goes into the sous's prompt, and any member can publish an alias: only plain ones do.
SWARM_ROOM = re.compile(r"#[A-Za-z0-9._=/+-]+:[A-Za-z0-9.-]+(:[0-9]+)?|![A-Za-z0-9._=/+-]+(:[A-Za-z0-9.-]+(:[0-9]+)?)?")


def swarm_rooms(account: dict) -> list[str]:
    """The account's chat rooms, as swarm lists them: by alias, or by bare ID
    (`!…`) when a room has none, as a DM doesn't; join takes either. Spaces are
    left out: swarm refuses them as unencrypted. A room whose name isn't plain,
    or that can't be looked up, is skipped, loudly."""
    listed = subprocess.run([swarm_binary(), "accounts", "list"], capture_output=True, text=True, check=True)
    row = next(a for a in json.loads(listed.stdout)["accounts"] if a["account"] == account["user_id"])
    if row["rooms"] is None:  # swarm couldn't read the account, as when its token was revoked
        print(f"kitchen: swarm can't list {account['user_id']}'s rooms ({row.get('error')}); "
              "the sous launches without joining them", file=sys.stderr)
        return []
    rooms = row["rooms"]

    joinable = []
    for room in rooms:
        try:
            if not SWARM_ROOM.fullmatch(room):
                raise ValueError("not a plain room name")
            room_id = room if room.startswith("!") else matrix_get(account, f"/directory/room/{quote(room, safe='')}")["room_id"]
            if matrix_get(account, f"/rooms/{quote(room_id, safe='')}/state/m.room.create").get("type") != "m.space":
                joinable.append(room)
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f"kitchen: swarm room {room!r} can't be looked up, so the sous won't join it ({e!r})", file=sys.stderr)
    return joinable


def swarm_held(account: str) -> bool:
    """Whether a session holds `account`, which it does once it has joined."""
    listed = subprocess.run([swarm_binary(), "accounts", "list"], capture_output=True, text=True, check=True)
    return any(a["account"] == account and a["holder"] for a in json.loads(listed.stdout)["accounts"])


def swarm_credential(account: str) -> dict:
    """The credential swarm keeps on this machine for `account`."""
    for path in (Path.home() / ".swarm" / "accounts").glob("*/*/credential.json"):
        credential = json.loads(path.read_text())
        if credential["user_id"] == account:
            return credential
    sys.exit(f"no swarm account {account} on this machine (swarm accounts add it first).")


def swarm_join_prompt(user_id: str, rooms: list[str]) -> str:
    """The first turn of a swarm session: messages reach it only from the rooms
    it joins, and every launch is a new session."""
    return f"Call the swarm `join` tool as account {user_id} once for each of: {', '.join(rooms)}. Say nothing else."


# The first swarm build to receive an account's saved rooms and DMs again by itself.
SWARM_RESUMES = 156


def swarm_build() -> int:
    """The installed swarm's build number: 156 in `0.1.9 (156, commit …)`."""
    out = subprocess.run([swarm_binary(), "--version"], capture_output=True, text=True, check=True).stdout
    return int(re.search(r"\((\d+),", json.loads(out)["version"])[1])


def sous_first_turn(kitchen: str) -> list[str]:
    """The sous's launch prompt: the rooms to join of each account swarm won't
    receive again by itself. A swarm that resumes does so for every account
    it has saved rooms for (its `receiving.json`), and a rejoin there would
    only spend the sous's context."""
    links = swarm_links(kitchen)
    resumes = bool(links) and swarm_build() >= SWARM_RESUMES
    joins = []
    for link in links:
        if resumes and (link.resolve().parent / "receiving.json").exists():
            continue
        account = json.loads(link.read_text())
        if rooms := swarm_rooms(account):
            joins.append(swarm_join_prompt(account["user_id"], rooms))
    if links:
        joins.extend(receive_prompts(kitchen, links[0]))
    return [" ".join(joins)] if joins else []


def receive_prompts(kitchen: str, link: Path) -> list[str]:
    """What brings the kitchen's own account in line with the `receive` list in
    its project's `.kitchen/config.toml`: a disconnect from each named room it
    left out, and an unmute of each it lists that swarm has muted. Swarm keeps a
    mute across restarts, so a list already in force asks nothing. Rooms without
    a name, its DMs, are always received, and a mention reaches it anywhere. No
    config, or no `receive` in it, leaves the account's mutes as they are."""
    config = Path(json.loads((state_dir(kitchen) / "kitchen.json").read_text())["source"]) / ".kitchen" / "config.toml"
    receive = tomllib.loads(config.read_text()).get("receive") if config.exists() else None
    if receive is None:
        return []
    muted_file = link.resolve().parent / "muted"
    muted = set(json.loads(muted_file.read_text()).values()) if muted_file.exists() else set()
    account = json.loads(link.read_text())
    named = [room for room in swarm_rooms(account) if room.startswith("#")]
    prompts = []
    if off := [room for room in named if room not in receive and room not in muted]:
        prompts.append(f"Call the swarm `disconnect` tool once for each of: {', '.join(off)}. Say nothing else.")
    if on := [room for room in named if room in receive and room in muted]:
        prompts.append(f"Call the swarm `join` tool as account {account['user_id']} with unmute: true "
                       f"once for each of: {', '.join(on)}. Say nothing else.")
    return prompts


def channel_servers(kitchen: str) -> dict:
    """The channel servers a sous loads: the kitchen's own, plus plow-chat or
    Discord once this kitchen holds its credential. All run the `kitchen` on
    PATH, the deployed install."""
    servers = {"kitchen": {"command": "kitchen", "args": ["channel-server", kitchen]}}
    if (plow_chat_dir(kitchen) / "plow-credentials").exists():
        servers["plow-chat"] = {"command": "kitchen", "args": ["plow-chat-server", kitchen]}
    if (state_dir(kitchen) / "discord" / ".env").exists():
        # Static access: only the allowlist on disk at boot is heard, never paired or edited live.
        env = {"DISCORD_STATE_DIR": str(state_dir(kitchen) / "discord"), "DISCORD_ACCESS_MODE": "static"}
        servers["discord"] = {"command": "kitchen", "args": ["discord-server"], "env": env}
    return servers


def exec_discord_server():
    for name, digest in DISCORD_SHA256.items():
        if hashlib.sha256((DISCORD_PLUGIN / name).read_bytes()).hexdigest() != digest:
            sys.exit(f"discord: {DISCORD_PLUGIN / name} is not the reviewed 0.0.4 copy; refusing to start")
    os.execvp("bun", ["bun", "run", "--cwd", str(DISCORD_PLUGIN), "--shell=bun", "--silent", "start"])


def channel_flags(kitchen: str) -> list[str]:
    return [f"server:{name}" for name in channel_servers(kitchen)]


def atomic_write_json(path: Path, data: dict, mode: int = 0o600):
    """Atomic JSON write via temp-then-os.replace. Concurrent writers
    never share a temp filename and never replace each other's in-flight
    file. Readers see either the previous or the new full file — never
    empty/partial.

    Creates the parent directory if missing. On any failure (serialization,
    write, replace) the orphaned tempfile is unlinked before the exception
    re-raises, so repeated failures don't accumulate stale .tmp files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            if not WINDOWS:  # a Windows file takes its profile folder's ACL
                os.fchmod(f.fileno(), mode)
            f.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def write_status(base: Path, name: str, data: dict):
    # Eliminates the JSONDecodeError race for brigade / statusline-segment;
    # cmd_ticket + UserPromptSubmit hook for Claude cooks both fire writes.
    atomic_write_json(base / "cooks" / f"{name}.json", data)


def read_status(base: Path, name: str) -> Optional[dict]:
    try:  # a cook clocking out can remove its file at any moment
        return json.loads((base / "cooks" / f"{name}.json").read_text())
    except FileNotFoundError:
        return None


def update_status(base: Path, name: str, **fields):
    """Read existing status, merge in `fields`, write atomically.

    Use this for status transitions that must not silently clear durable
    fields like `tokens` and `backend`. Initial-write callers (cmd_hire's
    booting/failed writes) should call write_status directly — there's
    nothing to preserve before the first write.
    """
    current = read_status(base, name) or {}
    current.update(fields)
    write_status(base, name, current)


def chat_record(message: str, **fields) -> dict:
    """A chat message the sous still owes an answer: `message` is
    '<source> <chat> <id>'. A cook carries one as `chat_message`; waits.json
    holds the ones waiting on people outside the kitchen (`on`, `note`)."""
    return {"message": message, "opened_at": datetime.now(timezone.utc).isoformat(), **fields}


def read_waits(base: Path) -> list:
    path = base / "waits.json"
    return json.loads(path.read_text()) if path.exists() else []


def write_waits(base: Path, waits: list):
    atomic_write_json(base / "waits.json", waits)


def _age(opened_at: str) -> str:
    s = int((datetime.now(timezone.utc) - datetime.fromisoformat(opened_at)).total_seconds())
    return f"{s // 86400}d" if s >= 86400 else f"{s // 3600}h" if s >= 3600 else f"{s // 60}m"


def with_digest(base: Path, content: str) -> str:
    """Prefix a notification to the sous with what it still owes, oldest first."""
    cooks = sorted(((s["chat_message"], name) for name in (f.stem for f in (base / "cooks").glob("*.json"))
                    if "chat_message" in (s := read_status(base, name) or {})), key=lambda c: c[0]["opened_at"])
    waits = sorted(read_waits(base), key=lambda w: w["opened_at"])
    lines = []
    if cooks:
        lines += ["♨️ Waiting on cooks:"] + [f"- {cook} · {r['message']} · {_age(r['opened_at'])}" for r, cook in cooks]
    if waits:
        lines += ["🍗 Waiting on others:"] + [
            f"- {w['on']} · {w['message']}{' · ' + w['note'] if w['note'] else ''} · {_age(w['opened_at'])}" for w in waits]
    return "\n".join(lines + ["", content]) if lines else content


def project_slug(project_path: Path) -> str:
    """Derive a project slug from `git config --get remote.origin.url`.

    Returns the bare repo name — the last path component of the remote,
    sans `.git`. `git@github.com:owner/my-project.git` -> `my-project`.
    Two unrelated repos that share a repo name collide; that's the user's
    problem to disambiguate with an explicit `--kitchen` / kitchen name.
    With no remote, falls back to a slugification of the absolute repo
    toplevel path — or of the path itself outside git, so a plain directory
    (a home kitchen) works too. Collision-proof across unrelated directories
    on one machine; loses cross-machine identity.
    """
    result = subprocess.run(
        ["git", "-C", str(project_path), "config", "--get", "remote.origin.url"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return _slug_from_path(project_path)
    url = result.stdout.strip()
    if url.endswith(".git"):
        url = url[:-4]

    if ":" in url and "://" not in url:
        _, _, path_part = url.partition(":")
    elif "://" in url:
        _, _, path_part = url.split("://", 1)[1].partition("/")
    else:
        sys.exit(f"Could not parse remote URL: {url}")

    parts = [p for p in path_part.split("/") if p]
    if len(parts) < 2:
        sys.exit(f"Could not derive owner/repo from remote URL: {url}")

    return parts[-1]


def namespaced(project: Path, requested: str) -> str:
    """Kitchen name scoped by project slug: `<slug>-<requested>`.

    The single source of truth for the namespaced-name formula, so the
    open path and the lookup probe (resolve_kitchen) always agree.

    Idempotent: a `requested` name that is already slug-scoped is returned
    untouched, so the formula never stacks prefixes. This collapses the
    common `kitchen open` no-name case (dir name == slug → `seed-domo`, not
    `seed-domo-seed-domo`) and stops re-opens / opening from a worktree dir
    already named for the kitchen from piling slug on slug (the on-disk
    `my-project-my-project-my-project-feature-x` triple). The `f"{slug}-"`
    boundary keeps a mere prefix match (`my-projecty`) from being mistaken for
    an already-scoped name.
    """
    slug = project_slug(project)
    if requested == slug or requested.startswith(f"{slug}-"):
        return requested
    return f"{slug}-{requested}"


def _slug_from_path(project_path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(project_path), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    path = result.stdout.strip() or str(project_path.resolve())
    slug = re.sub(r"[^a-z0-9-]+", "-", path.lower()).strip("-")
    if not slug:
        sys.exit(f"Could not derive a slug from {path!r}.")
    return slug
