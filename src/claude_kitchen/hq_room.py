"""Retire one HQ room for everyone, for good (irreversible), history kept, on the approver's 🗑️.

Agents hold no admin on HQ: they assume `swarm-hq-room` (AWS profile of that
name), which may only invoke the `swarm-hq-room` function in plow's terraform.
The function records the retire prompt (`ask`) and, on `retire`, checks the
approver's 🗑️ on it, the keep list and the rest, retires the room with purge off, reads
it back, and logs every action where agents can't edit it.
"""
import json
import os
import subprocess
import tempfile

FUNCTION = PROFILE = "swarm-hq-room"


def invoke(action: str, room: str, event_id: str) -> dict:
    """The function's answer to one `ask` or `retire`; it validates everything."""
    payload = {"action": action, "room": room, "event": event_id, "by": os.environ.get("AGENT_NAME")}
    with tempfile.NamedTemporaryFile() as out:
        subprocess.run(["aws", "lambda", "invoke", "--profile", PROFILE, "--region", "us-west-2", "--function-name", FUNCTION,
                        "--cli-binary-format", "raw-in-base64-out", "--payload", json.dumps(payload), out.name],
                       check=True, capture_output=True, timeout=180)
        return json.load(open(out.name))
