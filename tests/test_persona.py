"""The persona session keeps swarm's confinement and adds only the persona."""
from claude_kitchen.persona import card_url, launch_argv

# What `swarm persona` 0.1.9 prints, for a directory with an apostrophe in it.
PRINTED = (
    "cd '/home/o'\\''k/.claude-kitchen/boss/swarm/cedar-public' && claude "
    "--dangerously-load-development-channels server:swarm --strict-mcp-config --mcp-config .mcp.json "
    "--setting-sources project --tools '' --allowedTools mcp__swarm__reply --permission-mode dontAsk\n"
)


def test_persona_session_runs_swarms_command_in_its_directory_with_the_persona_appended():
    cwd, argv = launch_argv(PRINTED, "/p/persona.md")
    assert cwd == "/home/o'k/.claude-kitchen/boss/swarm/cedar-public"
    assert argv == [
        "claude", "--dangerously-load-development-channels", "server:swarm",
        "--strict-mcp-config", "--mcp-config", ".mcp.json", "--setting-sources", "project",
        "--tools", "", "--allowedTools", "mcp__swarm__reply", "--permission-mode", "dontAsk",
        "--append-system-prompt-file", "/p/persona.md",
    ]


def test_card_url_is_the_agents_public_card_on_its_company():
    credential = {"homeserver": "https://plow.swarm.plow.co/", "user_id": "@cedar:plow.swarm.plow.co"}
    assert card_url(credential) == "https://plow.swarm.plow.co/a2a/cedar/.well-known/agent-card.json"
