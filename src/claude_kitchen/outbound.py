"""Approve an outside host for a Swarm agent on this machine, as its owner's delegate.

Swarm keeps the hosts an account's owner approved in the account's
`outbound.json`, and a call waiting for approval re-reads that list every few
seconds, so a host added here lets it through. Writing the file takes no Swarm
credential, only the access to the account's folder that already holds one.
A hand-off to a new owner clears the list, as it clears the owner's own approvals.
"""
import json
import os
import re
from pathlib import Path

ACCOUNT = re.compile(r"@([a-z0-9][a-z0-9._=-]*):([A-Za-z0-9][A-Za-z0-9.-]*(?::\d+)?)")
HOST = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?")


def approve_host(account: str, host: str, customers: set[str], home: Path) -> dict:
    """Add `host` to `account`'s approved hosts and close its open ask for it.
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
    if host not in saved["approved"]:
        saved["approved"].append(host)
    saved["asks"].pop(host, None)
    fresh = ledger.with_name("outbound.json.approve-host")
    fresh.write_text(json.dumps(saved))
    os.replace(fresh, ledger)
    return {"account": account, "host": host, "room": f"#{name}-outbound:{server}",
            "line": f"Approved {host} for {name}, as its owner's delegate."}


def customer_agents(operator_env: Path) -> set[str]:
    """The operator's customer agents (`CUSTOMER_AGENTS`, ids or names, space-separated)."""
    for line in operator_env.read_text().splitlines():
        if line.startswith("CUSTOMER_AGENTS="):
            return set(line.split("=", 1)[1].strip("\"'").split())
    raise SystemExit(f"{operator_env} sets no CUSTOMER_AGENTS: refusing to approve for any agent.")
