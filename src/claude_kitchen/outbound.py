"""Approve an outside host for a Swarm agent on this machine, as its owner's delegate.

Swarm keeps the hosts an account's owner approved in the account's
`outbound.json`, and a call waiting for approval re-reads that list every few
seconds, so a host added here lets it through. Writing the file takes no Swarm
credential, only the access to the account's folder that already holds one.
A hand-off to a new owner clears the list, as it clears the owner's own approvals.
"""
import json
import re
from pathlib import Path

from claude_kitchen.state import atomic_write_json

ACCOUNT = re.compile(r"@([a-z0-9][a-z0-9._=-]*):([A-Za-z0-9][A-Za-z0-9.-]*(?::\d+)?)")
HOST = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?")


def approve_host(account: str, host: str, customers: set[str], operator: str, home: Path) -> dict:
    """Add `host` to `account`'s approved hosts and close its open ask for it,
    only for an agent the server records as the `operator`'s (on any server).
    Returns the agent's outbound room and the line that logs the approval."""
    match = ACCOUNT.fullmatch(account)
    if not match:
        raise SystemExit(f"{account} is not a Swarm account id (@name:server).")
    name, server = match.groups()
    if customers & {account, name}:
        raise SystemExit(f"{account} is a customer's agent: inspect-only, never approved for.")
    host = host.lower()
    if not HOST.fullmatch(host):
        raise SystemExit(f"{host} is not a bare host name (no scheme, port or path).")
    ledger = home / "accounts" / server / name / "outbound.json"
    if not ledger.exists():
        raise SystemExit(f"No {ledger}: {account} has never called out from this machine.")
    saved = json.loads(ledger.read_text())
    owner = saved.get("owner") or "nobody"
    if owner.split(":")[0] != operator.split(":")[0]:
        raise SystemExit(f"{account}'s recorded owner is {owner}, not {operator}: refusing.")
    if host not in saved["approved"]:
        saved["approved"].append(host)
    saved["asks"].pop(host, None)
    atomic_write_json(ledger, saved)
    return {"account": account, "host": host, "room": f"#{name}-outbound:{server}",
            "line": f"Approved {host} for {name}, as its owner's delegate."}


def operator_setting(operator_env: Path, key: str) -> str:
    """`key`'s value in the operator's env file; unset refuses every approval."""
    for line in operator_env.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip("\"'")
    raise SystemExit(f"{operator_env} sets no {key}: refusing to approve for any agent.")
