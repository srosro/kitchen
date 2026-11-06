"""The persona session keeps swarm's confinement and adds only the persona and its join."""
from claude_kitchen.persona import card_url, launch_argv

# The `launch` that `swarm accounts add cedar-public` returns, for a directory with an apostrophe in it.
LAUNCH = {
    "dir": "/home/o'k/.swarm/accounts/plow.swarm.plow.co/cedar-public/session",
    "command": "cd '/home/o'\\''k/.swarm/accounts/plow.swarm.plow.co/cedar-public/session' && claude "
               "--strict-mcp-config --mcp-config .mcp.json --setting-sources project --tools '' "
               "--allowedTools mcp__swarm__join,mcp__swarm__send --permission-mode dontAsk",
}


def test_persona_session_runs_swarms_command_with_the_persona_and_its_first_turn_appended():
    assert launch_argv(LAUNCH, "/p/persona.md", "join") == [
        "claude", "--strict-mcp-config", "--mcp-config", ".mcp.json", "--setting-sources", "project",
        "--tools", "", "--allowedTools", "mcp__swarm__join,mcp__swarm__send", "--permission-mode", "dontAsk",
        "--append-system-prompt-file", "/p/persona.md", "join",
    ]


def test_card_url_is_the_agents_public_card_on_its_company():
    credential = {"homeserver": "https://plow.swarm.plow.co/", "user_id": "@cedar:plow.swarm.plow.co"}
    assert card_url(credential) == "https://plow.swarm.plow.co/a2a/cedar/.well-known/agent-card.json"
