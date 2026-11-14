"""The persona session keeps swarm's confinement and adds only the persona and its join."""
from claude_kitchen.persona import card_url, launch_argv, public_account

# The `launch` that `swarm accounts add cedar-public` returns, for a directory with an apostrophe in it.
LAUNCH = {
    "dir": "/home/o'k/.swarm/accounts/plow.swarm.example.com/cedar-public/session",
    "command": "cd '/home/o'\\''k/.swarm/accounts/plow.swarm.example.com/cedar-public/session' && claude "
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
    credential = {"homeserver": "https://plow.swarm.example.com/", "user_id": "@cedar:plow.swarm.example.com"}
    assert card_url(credential) == "https://plow.swarm.example.com/a2a/cedar/.well-known/agent-card.json"


def test_public_account_is_named_in_full_so_a_name_on_two_servers_resolves():
    assert public_account("@cos:plow.swarm.example.com") == "@cos-public:plow.swarm.example.com"
