"""Retire one HQ room for everyone, for good (irreversible), history kept, through HQ's ops account.

Agents can't delete a room on HQ, and its owners' Delete room refuses agents,
so this runs the one admin call the ops account's how-to describes: an ECS
exec into HQ's synapse task (AWS profile `plow`), where `hq_room_task.py`
logs in as `@plow` from the task's environment, checks the approver's 🗑️ on
the prompt `ask` recorded, retires the room with purge off, reads it back and
logs out. Purge is not offered: it needs a person's written go and a hand-run
call. Who approves, which agents may prompt, and which rooms are kept come
from the operator's private settings. Each use is appended to a private log.
"""
import base64
import getpass
import json
import os
import re
import subprocess
import time
import zlib
from pathlib import Path

from claude_kitchen.outbound import operator_setting
from claude_kitchen.state import state_root

CLUSTER, SERVICE, CONTAINER = "swarm", "company-hq", "synapse"
AWS = {"AWS_PROFILE": "plow", "AWS_REGION": "us-west-2"}
# HQ's room ids carry no server (room version 12); an alias always does.
TARGET = re.compile(r"![A-Za-z0-9._=/+-]+(:plow\.co)?|#[A-Za-z0-9._=/+-]+:plow\.co")
EVENT = re.compile(r"\$[A-Za-z0-9._=/+-]+")
OPERATOR_ENV = Path.home() / ".config" / "kitchen" / "operator.env"


def rules() -> dict:
    """The operator's private settings: the one account whose 🗑️ confirms, the
    agents whose prompt it must answer, and the rooms that stay whoever asks."""
    setting = lambda key: operator_setting(OPERATOR_ENV, key).split()
    approver, = setting("HQ_ROOM_APPROVER")
    return {"approver": approver, "agents": setting("HQ_ROOM_AGENTS"), "keep": setting("HQ_ROOM_KEEP")}


def log_path() -> Path:
    return state_root() / "hq-room.log"


def private_write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, mode | os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
        f.write(text)


def asks() -> dict:
    path = state_root() / "hq-room-asks.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save_asks(recorded: dict) -> None:
    private_write(state_root() / "hq-room-asks.json", json.dumps(recorded), os.O_TRUNC)


def ask(target: str, prompt: str) -> dict:
    """Record `prompt`, the event id of the retire prompt the agent just sent in
    `target` with swarm's send (kitchen can't post into an encrypted room), as
    the one the approver's 🗑️ must answer. A later ask for the room replaces it."""
    check(target, prompt, "the prompt you sent")
    recorded = {"prompt": prompt, "at": int(time.time() * 1000)}
    save_asks({**asks(), target: recorded})
    append_log("ask", prompt, target, recorded)
    return recorded


def task_command(target: str, asked_in: str, rules: dict) -> str:
    """The container's command: the task script with its call appended,
    compressed and base64'd, since the exec shell-parses what it is given."""
    source = Path(__file__).with_name("hq_room_task.py").read_text() + f"\nmain({json.dumps(target)}, {json.dumps(asked_in)}, {json.dumps(rules)})\n"
    packed = base64.b64encode(zlib.compress(source.encode(), 9)).decode()
    return f"""python3 -c 'import base64,zlib;exec(zlib.decompress(base64.b64decode("{packed}")))'"""


def run_in_task(command: str) -> str:
    env = {**os.environ, **AWS}
    task = subprocess.run(["aws", "ecs", "list-tasks", "--cluster", CLUSTER, "--service-name", SERVICE,
                           "--desired-status", "RUNNING", "--query", "taskArns[0]", "--output", "text"],
                          env=env, capture_output=True, text=True, check=True).stdout.strip()
    if not task.startswith("arn:"):
        raise SystemExit(f"No running {SERVICE} task in cluster {CLUSTER}.")
    done = subprocess.run(["aws", "ecs", "execute-command", "--cluster", CLUSTER, "--task", task,
                           "--container", CONTAINER, "--interactive", "--command", command],
                          env=env, capture_output=True, text=True, timeout=180)
    return done.stdout + done.stderr


def parse(output: str) -> dict:
    for line in output.splitlines():
        if line.strip().startswith("HQ_ROOM "):
            return json.loads(line.strip()[len("HQ_ROOM "):])
    raise SystemExit("The task printed no result; its output ends:\n" + "\n".join(output.splitlines()[-15:]))


def append_log(action: str, event_id: str, target: str, result: dict) -> None:
    private_write(log_path(), json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "by": os.environ.get("AGENT_NAME") or getpass.getuser(),
                                          "action": action, "event": event_id, "target": target, "result": result}) + "\n", os.O_APPEND)


def check(target: str, event_id: str, what: str) -> None:
    if not TARGET.fullmatch(target):
        raise SystemExit(f"{target} is not an HQ room: give one !roomid or #alias:plow.co.")
    if not EVENT.fullmatch(event_id):
        raise SystemExit(f"{event_id} is not an event id ($...): give {what}.")


def retire(target: str, asked_in: str) -> dict:
    """Retire `target` on the approver's 🗑️ `asked_in`. The recorded ask is used
    up by the attempt, whatever its result."""
    check(target, asked_in, "the approver's 🗑️ reaction")
    recorded = asks()
    if target not in recorded:
        raise SystemExit(f"No recorded ask for {target}: send the prompt, then run `kitchen hq-room ask {target} --prompt <its event id>`.")
    pending = recorded.pop(target)
    save_asks(recorded)
    result = {"error": "no result from the task"}
    try:
        result = parse(run_in_task(task_command(target, asked_in, {**rules(), "ask": pending})))
        return result
    finally:
        append_log("retire (irreversible)", asked_in, target, result)
