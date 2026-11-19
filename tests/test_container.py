import argparse
import io
import json
from pathlib import Path
from unittest.mock import call, patch
import pytest

from claude_kitchen import container
from claude_kitchen.cli import _reopen_cmd, _sous_session_id, cmd_hook
from claude_kitchen.state import channel_socket
from claude_kitchen.spawn import spawn_sous


def write_config(project: Path, home: Path, mount: Path):
    (project / ".kitchen").mkdir()
    (project / ".kitchen" / "container.toml").write_text(f'''\
[container]
image = "agent:local"
network = "agents"
home_source = "{home}"
wiki = "{project.parent / 'wiki'}"
[container.environment]
NODE_ENV = "production"
[container.environment_paths]
PERSONAL_RUNBOOK = "{mount}"
[[container.mounts]]
source = "{mount}"
target = "{mount}"
read_only = true
''')


def test_container_config_rejects_kitchen_runtime_environment(tmp_path):
    home = tmp_path / "home"
    (tmp_path / ".kitchen").mkdir()
    (tmp_path / ".kitchen" / "container.toml").write_text(f'''\
[container]
image = "agent:local"
network = "agents"
home_source = "{home}"

[container.environment]
HOME = "/tmp/redirected"
''')

    with pytest.raises(SystemExit, match="reserved"):
        container.config(tmp_path)


def test_container_run_uses_the_locked_down_boundary(tmp_path, monkeypatch):
    project, home, mount = tmp_path / "project", tmp_path / "home", tmp_path / "role"
    project.mkdir()
    mount.mkdir()
    (tmp_path / "kitchen.json").write_text("{}\n")
    write_config(project, home, mount)
    monkeypatch.setattr(Path, "home", lambda: Path("/home/operator"))
    monkeypatch.setattr("claude_kitchen.state.state_root", lambda: tmp_path / "state-root")
    monkeypatch.setattr(container.os, "getuid", lambda: 1000)
    monkeypatch.setattr(container.os, "getgid", lambda: 1000)
    results = [
        type("R", (), {"returncode": 0, "stdout": ""})(),
        type("R", (), {"returncode": 0, "stdout": "acme-scout\n", "stderr": ""})(),
        type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
        type("R", (), {"returncode": 0, "stdout": ""})(),
        type("R", (), {"returncode": 0, "stdout": ""})(),
        type("R", (), {"returncode": 0, "stdout": "false\n"})(),
    ]
    with patch("claude_kitchen.container.subprocess.run", side_effect=results) as run:
        cfg = container.config(project)
        container.run("acme-scout", project, tmp_path, cfg,
                      ["claude", "--version"], {"HOME": "/home/operator"})
    argv = run.call_args_list[3].args[0]
    assert argv[:3] == ["docker", "run", "--detach"]
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges:true" in argv
    assert "--restart" not in argv
    assert argv[argv.index("--user") + 1] == "1000:1000"
    assert argv[argv.index("--network") + 1] == "agents"
    assert argv[argv.index("--label") + 1] == "com.srosro.kitchen.name=acme-scout"
    assert "--privileged" not in argv
    assert "--net=host" not in argv and "--pid=host" not in argv and "--ipc=host" not in argv
    specs = [argv[i + 1] for i, value in enumerate(argv) if value == "--mount"]
    assert f"type=bind,source={home},target=/home/operator" in specs
    assert f"type=bind,source={tmp_path},target={tmp_path},readonly" in specs
    assert f"type=bind,source={tmp_path / 'container-session.json'},target={tmp_path / 'container-session.json'}" in specs
    assert f"type=bind,source={tmp_path / 'notes'},target={tmp_path / 'notes'}" in specs
    assert f"type=bind,source={channel_socket(tmp_path).parent},target=/tmp/kitchen-channel" in specs
    assert channel_socket(tmp_path).parent.parent.name == "sockets"
    assert f"type=bind,source={tmp_path / 'wiki'},target={tmp_path / 'wiki'}" in specs
    assert f"type=bind,source={mount},target={mount},readonly" in specs
    assert "PERSONAL_RUNBOOK=" + str(mount) in argv
    assert "NODE_ENV=production" in argv
    assert "KITCHEN_CONTAINER=1" in argv
    assert run.call_args_list[0] == call(
        ["docker", "network", "inspect", "agents"], check=True, stdout=container.subprocess.DEVNULL,
    )
    monkeypatch.setenv("KITCHEN_CHANNEL_DIR", "/tmp/kitchen-channel")
    assert channel_socket(tmp_path) == Path("/tmp/kitchen-channel/kitchen.sock")


def test_prepare_mount_target_keeps_home_parent_writable(tmp_path, monkeypatch):
    source = tmp_path / "credential.json"
    source.write_text("{}\n")
    home_source = tmp_path / "container-home"
    home_source.mkdir()
    monkeypatch.setattr(Path, "home", lambda: Path("/home/u"))

    container._prepare_mount_target(home_source, {
        "source": source,
        "target": Path("/home/u/.claude/credential.json"),
    })

    assert (home_source / ".claude").is_dir()


def test_remove_refuses_a_container_kitchen_does_not_own():
    foreign = type("R", (), {"returncode": 0, "stdout": "someone-else\n", "stderr": ""})()
    with patch("claude_kitchen.container.subprocess.run", return_value=foreign) as run:
        with pytest.raises(SystemExit, match="does not own"):
            container.remove("acme-scout")
    assert len(run.call_args_list) == 1


def test_remove_accepts_docker_lowercase_not_found():
    missing = type("R", (), {
        "returncode": 1,
        "args": ["docker", "inspect"],
        "stdout": "",
        "stderr": "error: no such object: kitchen-acme-scout\n",
    })()
    with patch("claude_kitchen.container.subprocess.run", return_value=missing) as run:
        container.remove("acme-scout")
    assert len(run.call_args_list) == 1


def test_container_mcp_uses_binaries_inside_the_image(tmp_path):
    (tmp_path / "kitchen-mcp.json").write_text(json.dumps({"mcpServers": {
        "kitchen": {"command": "kitchen", "args": ["channel-server", "x"]},
        "swarm": {"command": "/host/bin/swarm", "args": ["mcp", "claude"]},
    }}))
    path = container.mcp_config(tmp_path)
    assert json.loads(path.read_text()) == {"mcpServers": {
        "kitchen": {"command": "kitchen", "args": ["channel-server", "x"]},
        "swarm": {"command": "swarm", "args": ["mcp", "claude"]},
    }}


def test_container_hook_keeps_host_lifecycle_metadata_read_only(tmp_path, monkeypatch):
    host_state = {"source": "/host/project", "sous_container": True, "sous_session_id": "host-session"}
    (tmp_path / "kitchen.json").write_text(json.dumps(host_state) + "\n")
    monkeypatch.setenv("AGENT_NAME", "sous")
    monkeypatch.setenv("AGENT_KITCHEN", "acme-scout")
    monkeypatch.setenv("STATUS_DIR", str(tmp_path))
    monkeypatch.setenv("KITCHEN_CONTAINER", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "hook_event_name": "Stop", "session_id": "session-1",
    })))

    with patch("claude_kitchen.cli._login_alert"), \
            patch("claude_kitchen.cli._record_pause"), \
            patch("claude_kitchen.cli._warn_weekly_usage"):
        cmd_hook(argparse.Namespace(command="hook"))
    assert json.loads((tmp_path / "kitchen.json").read_text()) == host_state
    assert _sous_session_id(tmp_path, host_state) == "session-1"


def test_container_reopen_resumes_the_isolated_session(tmp_path, monkeypatch):
    monkeypatch.setattr("claude_kitchen.cli.state_dir", lambda kitchen: tmp_path)
    (tmp_path / "kitchen.json").write_text(json.dumps({"source": "/project", "sous_container": True, "sous_launch": {"argv": ["kitchen", "open"]}}))
    (tmp_path / "container-session.json").write_text('{"sous_session_id":"session-1"}')
    assert _reopen_cmd("acme-scout").endswith("kitchen open --resume")


def test_initial_container_move_does_not_resume_host_session(tmp_path):
    host_state = {"sous_container": False, "sous_session_id": "host-session"}
    assert _sous_session_id(tmp_path, host_state, containerized=True) is None


def test_container_session_does_not_fallback_to_host_session(tmp_path):
    (tmp_path / "container-session.json").write_text("{}\n")
    state = {"sous_container": True, "sous_session_id": "host-session"}
    assert _sous_session_id(tmp_path, state) is None


def test_spawn_sous_hands_only_its_runtime_to_the_container(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "kitchen.json").write_text('{"sous_container": false}\n')
    cfg = {"image": "agent:local", "network": "agents", "environment": {}}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(container, "config", lambda value: cfg)
    monkeypatch.setattr(container, "mcp_config", lambda value: tmp_path / "container-mcp.json")
    seen = {}
    monkeypatch.setattr(container, "run", lambda *args: seen.update(args=args))
    monkeypatch.setattr("claude_kitchen.spawn.channel_flags", lambda kitchen: ["server:kitchen"])

    spawn_sous("acme-scout", tmp_path, "role", project=project, slug="acme")
    kitchen, mounted_project, mounted_state, passed_cfg, argv, env = seen["args"]
    assert (kitchen, mounted_project, mounted_state, passed_cfg) == (
        "acme-scout", project, tmp_path, cfg,
    )
    assert argv[argv.index("--mcp-config") + 1] == str(tmp_path / "container-mcp.json")
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["hooks"]["Stop"][0]["hooks"][0]["command"] == "kitchen hook"
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "mcp__swarm.*__send"
    assert settings["statusLine"]["command"] == "kitchen statusline-command"
    assert "do not hire or ticket cooks" in argv[argv.index("--append-system-prompt") + 1].lower()
    assert env == {
        "AGENT_KITCHEN": "acme-scout",
        "AGENT_NAME": "sous",
        "STATUS_DIR": str(tmp_path),
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "KITCHEN_WIKI": str(tmp_path / ".claude-kitchen/projects/acme/wiki"),
        "KITCHEN_NOTES": str(tmp_path / ".claude-kitchen/acme-scout/notes"),
        "HOME": str(tmp_path),
    }
    assert json.loads((tmp_path / "kitchen.json").read_text())["sous_container"] is True


def test_spawn_sous_removes_owned_container_when_config_is_removed(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "kitchen.json").write_text('{"sous_container": true}\n')
    monkeypatch.setattr("claude_kitchen.spawn.channel_flags", lambda kitchen: ["server:kitchen"])

    with patch.object(container, "remove") as remove, \
            patch("claude_kitchen.spawn.os.execvp", side_effect=SystemExit), pytest.raises(SystemExit):
        spawn_sous("acme-scout", tmp_path, "role", project=project, slug="acme")
    remove.assert_called_once_with("acme-scout")
    assert json.loads((tmp_path / "kitchen.json").read_text())["sous_container"] is False


def test_failed_transition_keeps_container_cleanup_marker(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "kitchen.json").write_text('{"sous_container": true}\n')

    with patch.object(container, "remove", side_effect=RuntimeError("docker unavailable")), \
            pytest.raises(RuntimeError, match="docker unavailable"):
        spawn_sous("acme-scout", tmp_path, "role", project=project, slug="acme")
    assert json.loads((tmp_path / "kitchen.json").read_text())["sous_container"] is True


def test_restart_removes_old_container_before_parsing_replacement(tmp_path):
    project = tmp_path / "project"
    (project / ".kitchen").mkdir(parents=True)
    (project / ".kitchen" / "container.toml").write_text("not valid toml = [")
    (tmp_path / "kitchen.json").write_text('{"sous_container": true}\n')

    with patch.object(container, "remove") as remove, pytest.raises(container.tomllib.TOMLDecodeError):
        spawn_sous("acme-scout", tmp_path, "role", project=project, slug="acme")
    remove.assert_called_once_with("acme-scout")
    assert json.loads((tmp_path / "kitchen.json").read_text())["sous_container"] is False
