"""The persona session keeps swarm's confinement and adds only the persona and its join."""
import json

from claude_kitchen import persona
from claude_kitchen.persona import card_url, launch_argv

# The `launch` that `swarm accounts add cedar-public` returns, for a directory with an apostrophe in it.
LAUNCH = {
    "dir": "/home/o'k/.swarm/accounts/plow.swarm.plow.co/cedar-public/session",
    "command": "cd '/home/o'\\''k/.swarm/accounts/plow.swarm.plow.co/cedar-public/session' && claude "
               "--strict-mcp-config --mcp-config .mcp.json --setting-sources project --tools '' "
               "--allowedTools mcp__swarm__join,mcp__swarm__send --permission-mode dontAsk",
}


def test_persona_session_runs_swarms_command_with_the_persona_appended():
    assert launch_argv(LAUNCH, "/p/persona.md") == [
        "claude", "--strict-mcp-config", "--mcp-config", ".mcp.json", "--setting-sources", "project",
        "--tools", "", "--allowedTools", "mcp__swarm__join,mcp__swarm__send", "--permission-mode", "dontAsk",
        "--append-system-prompt-file", "/p/persona.md",
    ]


def test_card_url_is_the_agents_public_card_on_its_company():
    credential = {"homeserver": "https://plow.swarm.plow.co/", "user_id": "@cedar:plow.swarm.plow.co"}
    assert card_url(credential) == "https://plow.swarm.plow.co/a2a/cedar/.well-known/agent-card.json"


def test_the_persona_is_asked_to_join_until_its_account_is_held(tmp_path, monkeypatch):
    # A join given at launch is answered before swarm's server connects; the
    # account only counts as joined once `swarm accounts list` shows a holder.
    joined = tmp_path / "joined"
    listed = lambda holder: json.dumps({"accounts": [{"account": "@cos-public:p", "holder": holder}]})
    swarm = tmp_path / "swarm"
    swarm.write_text(f"#!/bin/sh\n[ -e {joined} ] && echo '{listed({'pid': 1})}' || echo '{listed(None)}'\n")
    swarm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setattr(persona, "JOIN_WAIT", 0)
    monkeypatch.setattr(persona, "pane_busy", lambda *a: False)
    typed = []
    monkeypatch.setattr(persona, "send_keys", lambda kitchen, window, text, backend: (typed.append(text), joined.touch()))
    persona.join("cos", "@cos-public:p", "join #cos-public")
    assert typed == ["join #cos-public"]
