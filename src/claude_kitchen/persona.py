"""A sous's public A2A persona: a session of its own, never the sous's.

`swarm accounts add <name>-public` sets up the public account's device and its
session directory and returns the confined command that runs it (swarm's tools
only, no shell, no user settings or memory files). Kitchen runs that command in the `persona`
window with the owner's persona file appended, while the agent is public."""
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request

from claude_kitchen.cli import _trust_dir
from claude_kitchen.state import state_dir, swarm_account, swarm_binary, swarm_join_prompt
from claude_kitchen.tmux import list_windows, has_session, tmux, target, join_swarm, SESSION

WINDOW = "persona"
POLL = 60


def persona_file(kitchen: str):
    return state_dir(kitchen) / "swarm" / "persona.md"


def card_url(credential: dict) -> str:
    name = credential["user_id"].removeprefix("@").split(":")[0]
    return f"{credential['homeserver'].rstrip('/')}/a2a/{name}/.well-known/agent-card.json"


def is_public(url: str) -> bool | None:
    """The directory's Make public switch is on exactly while the card is served;
    None when the gateway can't say, so a network blip changes nothing."""
    try:
        with urllib.request.urlopen(url, timeout=30):
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        print(f"{url}: {e}", file=sys.stderr)
    except OSError as e:  # unreachable or timed out
        print(f"{url}: {e}", file=sys.stderr)
    return None


def public_account(user_id: str) -> str:
    """The public account the bridge made for `user_id`, in full: swarm refuses a
    bare name held on more than one server here."""
    name, server = user_id.removeprefix("@").split(":", 1)
    return f"@{name}-public:{server}"


def launch_argv(launch: dict, persona: str) -> list[str]:
    """swarm's `cd '<dir>' && claude …` as argv, plus the persona."""
    claude = launch["command"].split(" && ", 1)[1]
    return [*shlex.split(claude), "--append-system-prompt-file", persona]


def start(kitchen: str):
    """Open the persona window unless it is already running. No persona file, no window."""
    if not persona_file(kitchen).exists():
        return
    if not has_session(kitchen):
        sys.exit(f"kitchen \"{kitchen}\" has no tmux session; open it first.")
    if WINDOW in list_windows(kitchen):
        return
    cmd = f"bash -lc {shlex.quote(f'exec kitchen persona-run {shlex.quote(kitchen)}')}"
    result = tmux("new-window", "-d", "-t", target(), "-n", WINDOW,
                  "-c", str(state_dir(kitchen)), cmd, kitchen=kitchen)
    if result.returncode:
        sys.exit(f"tmux: {result.stderr.strip()}")
    print(f"persona window started; it serves while the agent is public ({SESSION}:{WINDOW}).")


def run(kitchen: str):
    """The persona window's loop: wait for the switch, serve, stop when it is switched off."""
    account = swarm_account(kitchen)
    url = card_url(account)
    public = public_account(account["user_id"])
    while True:
        while is_public(url) is not True:
            time.sleep(POLL)
        added = subprocess.run([swarm_binary(), "accounts", "add", public], stdout=subprocess.PIPE, text=True)
        if added.returncode:
            time.sleep(POLL)
            continue
        added = json.loads(added.stdout)
        cwd = added["launch"]["dir"]
        argv = launch_argv(added["launch"], str(persona_file(kitchen)))
        _trust_dir(cwd, "claude")
        # The tmux server's environment can carry a sous's or cook's identity; the persona is neither.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("AGENT_", "KITCHEN_")) and k != "STATUS_DIR"}
        session = subprocess.Popen(argv, cwd=cwd, env=env)
        join_swarm(kitchen, WINDOW, added["account"], swarm_join_prompt(added["account"], added["rooms"]),
                   alive=lambda: session.poll() is None)
        while session.poll() is None:
            time.sleep(POLL)
            if is_public(url) is False:
                session.terminate()
                session.wait()
        time.sleep(POLL)  # a session that dies at once is not relaunched in a tight loop
