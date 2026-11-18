"""Retire one HQ room for everyone, for good (irreversible), history kept, through HQ's ops account.

Agents can't delete a room on HQ, and its owners' Delete room refuses agents,
so this runs the one admin call the ops account's how-to describes: an ECS
exec into HQ's synapse task (AWS profile `plow`), where `hq_room_task.py`
logs in as `@plow` from the task's environment, checks the approver's 🗑️ on
an agent's prompt in that room, retires it with purge off, reads it back and
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


def append_log(asked_in: str, target: str, result: dict) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "by": os.environ.get("AGENT_NAME") or getpass.getuser(),
                            "action": "retire (irreversible)", "asked_in": asked_in, "target": target, "result": result}) + "\n")


def retire(target: str, asked_in: str) -> dict:
    if not TARGET.fullmatch(target):
        raise SystemExit(f"{target} is not an HQ room: give one !roomid or #alias:plow.co.")
    if not EVENT.fullmatch(asked_in):
        raise SystemExit(f"{asked_in} is not an event id ($...): give the approver's 🗑️ reaction.")
    result = {"error": "no result from the task"}
    try:
        result = parse(run_in_task(task_command(target, asked_in, rules())))
        return result
    finally:
        append_log(asked_in, target, result)
