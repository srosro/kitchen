"""State management for claude-kitchen."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from urllib.request import Request, urlopen


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
    return state_root() / "projects" / slug / "wiki"


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


def swarm_account(kitchen: str) -> dict | None:
    """The credential of the swarm account this kitchen acts as: a link at
    `<state>/swarm/credential.json` to swarm's own copy under ~/.swarm/accounts."""
    link = state_dir(kitchen) / "swarm" / "credential.json"
    if not link.is_symlink():
        if link.exists():
            sys.exit(f"{link} is a pre-accounts swarm credential: move its account into "
                     "~/.swarm/accounts/<server>/<name>/ and link it back here (see the README).")
        return None
    return json.loads(link.read_text())  # a dangling link fails here, loudly


def mcp_servers(kitchen: str) -> dict:
    """What a sous loads: its channels, plus swarm once this kitchen has an
    account, which `swarm mcp claude` serves itself through Claude Code's inbox."""
    servers = channel_servers(kitchen)
    if account := swarm_account(kitchen):
        servers["swarm"] = {"command": swarm_binary(), "args": ["mcp", "claude", "--account", account["user_id"]]}
    return servers


SWARM_ALIAS = re.compile(r"#[A-Za-z0-9._=/+-]+:[A-Za-z0-9.-]+(:[0-9]+)?")


def swarm_rooms(account: dict) -> list[str]:
    """The account's chat rooms, each by a name `join` takes. swarm lists a
    room with no canonical alias by its bare ID (`!…`), which join would turn
    into a new room named after it; such a room is joined by the first alias
    the server publishes for it. Spaces are left out: swarm refuses them as
    unencrypted. A room that can't be named or looked up is skipped, loudly."""
    listed = subprocess.run([swarm_binary(), "accounts", "list"], capture_output=True, text=True, check=True)
    rooms = next(a["rooms"] for a in json.loads(listed.stdout)["accounts"] if a["account"] == account["user_id"])

    def get(path: str) -> dict:
        url = f"{account['homeserver'].rstrip('/')}/_matrix/client/v3{path}"
        with urlopen(Request(url, headers={"Authorization": f"Bearer {account['access_token']}"}), timeout=30) as r:
            return json.load(r)

    named = []
    for room in rooms:
        try:
            room_id = room if room.startswith("!") else get(f"/directory/room/{quote(room, safe='')}")["room_id"]
            if get(f"/rooms/{quote(room_id, safe='')}/state/m.room.create").get("type") == "m.space":
                continue
            names = [room] if room.startswith("#") else get(f"/rooms/{quote(room_id, safe='')}/aliases")["aliases"]
            # Any member can publish an alias, and it goes into the sous's prompt: only plain ones do.
            named.append(next(name for name in names if SWARM_ALIAS.fullmatch(name)))
        except (OSError, ValueError, KeyError, StopIteration, TypeError) as e:  # no plain alias, or a lookup failed
            print(f"kitchen: swarm room {room} has no alias the sous can join it by ({e!r})", file=sys.stderr)
    return named


def swarm_join_prompt(user_id: str, rooms: list[str]) -> str:
    """The first turn of a swarm session: messages reach it only from the rooms
    it joins, and every launch is a new session."""
    return f"Call the swarm `join` tool as account {user_id} once for each of: {', '.join(rooms)}. Say nothing else."


def sous_first_turn(kitchen: str) -> list[str]:
    """The sous's launch prompt, if it has swarm rooms to join."""
    if not (account := swarm_account(kitchen)):
        return []
    return [swarm_join_prompt(account["user_id"], swarm_rooms(account))]


def channel_servers(kitchen: str) -> dict:
    """The channel servers a sous loads: the kitchen's own, plus plow-chat,
    Buzz or Discord once this kitchen holds its credential. All run the
    `kitchen` on PATH, the deployed install."""
    servers = {"kitchen": {"command": "kitchen", "args": ["channel-server", kitchen]}}
    if (plow_chat_dir(kitchen) / "plow-credentials").exists():
        servers["plow-chat"] = {"command": "kitchen", "args": ["plow-chat-server", kitchen]}
    if (state_dir(kitchen) / "buzz" / "buzz-credentials").exists():
        servers["buzz"] = {"command": "kitchen", "args": ["buzz-server", kitchen]}
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
    path = base / "cooks" / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


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


def project_slug(project_path: Path) -> str:
    """Derive a project slug from `git config --get remote.origin.url`.

    Returns the bare repo name — the last path component of the remote,
    sans `.git`. `git@github.com:owner/my-project.git` -> `my-project`.
    Two unrelated repos that share a repo name collide; that's the user's
    problem to disambiguate with an explicit `--kitchen` / kitchen name.
    When no remote is configured but the path is a git repo, falls back to
    a slugification of the absolute repo toplevel path (collision-proof
    across unrelated repos on one machine; loses cross-machine identity).
    Fails loudly if the path is not a git repo.
    """
    result = subprocess.run(
        ["git", "-C", str(project_path), "config", "--get", "remote.origin.url"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return _slug_from_toplevel(project_path)
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


def _slug_from_toplevel(project_path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(project_path), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        sys.exit(
            f"{project_path} is not a git repository. "
            "Kitchen requires a git repo to derive a project slug."
        )
    toplevel = result.stdout.strip()
    slug = re.sub(r"[^a-z0-9-]+", "-", toplevel.lower()).strip("-")
    if not slug:
        sys.exit(f"Could not derive a slug from toplevel {toplevel!r}.")
    return slug
