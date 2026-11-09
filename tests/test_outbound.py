"""Tests for approving an outside host on a Swarm agent's behalf."""
import json

import pytest

from claude_kitchen.outbound import approve_host, customer_agents

ACCOUNT = "@ura:acme.example"
CUSTOMERS = {"@shop:acme.example", "patron"}


@pytest.fixture
def home(tmp_path):
    folder = tmp_path / "accounts" / "acme.example" / "ura"
    folder.mkdir(parents=True)
    ledger = {"threads": {"card": "$t"}, "approved": ["known.org"],
              "asks": {"new.org": "$ask", "other.net": "$other"}, "owner": "@boss=40acme.example:acme.example"}
    (folder / "outbound.json").write_text(json.dumps(ledger))
    return tmp_path


def test_the_host_is_approved_once_and_its_ask_closed(home):
    for host in ("New.org", "new.org"):
        result = approve_host(ACCOUNT, host, CUSTOMERS, home)
    saved = json.loads((home / "accounts" / "acme.example" / "ura" / "outbound.json").read_text())
    assert saved["approved"] == ["known.org", "new.org"]
    assert saved["asks"] == {"other.net": "$other"}
    assert saved["owner"] == "@boss=40acme.example:acme.example"
    assert result["room"] == "#ura-outbound:acme.example"
    assert "new.org" in result["line"]


@pytest.mark.parametrize("account, host, refusal", [
    ("@shop:acme.example", "new.org", "customer"),
    ("@patron:other.example", "new.org", "customer"),
    ("ura", "new.org", "not a Swarm account"),
    ("@x/../shop:acme.example", "new.org", "not a Swarm account"),
    (ACCOUNT, "https://new.org/card", "not a bare host"),
    ("@oak:acme.example", "new.org", "never called out"),
])
def test_refused(home, account, host, refusal):
    before = (home / "accounts" / "acme.example" / "ura" / "outbound.json").read_text()
    with pytest.raises(SystemExit, match=refusal):
        approve_host(account, host, CUSTOMERS, home)
    assert (home / "accounts" / "acme.example" / "ura" / "outbound.json").read_text() == before


@pytest.mark.parametrize("value", ['@shop:acme.example patron', '"@shop:acme.example patron"'])
def test_customer_agents_read_quoted_or_not(tmp_path, value):
    env = tmp_path / "operator.env"
    env.write_text(f"APPROVER=x\nCUSTOMER_AGENTS={value}\n")
    assert customer_agents(env) == CUSTOMERS
