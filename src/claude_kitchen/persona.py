"""A sous's public A2A persona: a session of its own, never the sous's.

`swarm persona` sets up the public account's device and its session directory
and prints the confined command that runs it (reply tool only, no shell, no
user settings or memory files). Kitchen runs that command in the `persona`
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
from claude_kitchen.state import state_dir
from claude_kitchen.tmux import list_windows, has_session, tmux, target, wait_for_prompt, SESSION

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
    except urllib.error.URLError as e:
        print(f"{url}: {e.reason}", file=sys.stderr)
    return None


def launch_argv(printed: str, persona: str) -> tuple[str, list[str]]:
    """`swarm persona`'s `cd '<dir>' && claude …` as (dir, argv), plus the persona."""
    cd, claude = printed.strip().split(" && ", 1)
    return shlex.split(cd)[1], [*shlex.split(claude), "--append-system-prompt-file", persona]


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
    credential_path = state_dir(kitchen) / "swarm" / "credential.json"
    url = card_url(json.loads(credential_path.read_text()))
    while True:
        while is_public(url) is not True:
            time.sleep(POLL)
        printed = subprocess.run(["swarm", "persona", "--credential", str(credential_path)],
                                 stdout=subprocess.PIPE, text=True)
        if printed.returncode:
            time.sleep(POLL)
            continue
        cwd, argv = launch_argv(printed.stdout, str(persona_file(kitchen)))
        _trust_dir(cwd, "claude")
        # The tmux server's environment can carry a sous's or cook's identity; the persona is neither.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("AGENT_", "KITCHEN_")) and k != "STATUS_DIR"}
        session = subprocess.Popen(argv, cwd=cwd, env=env)
        wait_for_prompt(kitchen, WINDOW, "claude")  # confirms the development-channels prompt
        while session.poll() is None:
            time.sleep(POLL)
            if is_public(url) is False:
                session.terminate()
                session.wait()
