"""Run a kitchen's sous in its configured Docker container."""
import json
import os
import subprocess
import tomllib
from pathlib import Path


MCP_CONFIG_NAME = "container-mcp.json"
OWNER_LABEL = "com.srosro.kitchen.name"
RESERVED_ENV = {
    "HOME", "AGENT_KITCHEN", "AGENT_NAME", "STATUS_DIR",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "KITCHEN_WIKI", "KITCHEN_NOTES",
    "KITCHEN_CHANNEL_DIR", "KITCHEN_CONTAINER",
}


def config(project: Path) -> dict | None:
    path = project / ".kitchen" / "container.toml"
    if not path.exists():
        return None
    value = tomllib.loads(path.read_text()).get("container")
    if value is None:
        return None
    value["home_source"] = _path(value["home_source"])
    value["home_source"].mkdir(parents=True, exist_ok=True, mode=0o700)
    value["mounts"] = [
        {**mount, "source": _path(mount["source"]), "target": _path(mount["target"])}
        for mount in value.get("mounts", [])
    ]
    missing = [str(mount["source"]) for mount in value["mounts"] if not mount["source"].exists()]
    if missing:
        raise SystemExit(f"container mount source does not exist: {missing[0]}")
    value["environment"] = dict(value.get("environment", {}))
    value["environment"].update({
        key: str(_path(path)) for key, path in value.get("environment_paths", {}).items()
    })
    reserved = RESERVED_ENV.intersection(value["environment"])
    if reserved:
        raise SystemExit(f"container environment name is reserved: {sorted(reserved)[0]}")
    if "wiki" in value:
        value["wiki"] = _path(value["wiki"])
        value["wiki"].mkdir(parents=True, exist_ok=True)
    return value


def _path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser().resolve()


def _prepare_mount_targets(mounts: list[dict]) -> None:
    for mount in mounts:
        ancestors = [
            candidate for candidate in mounts
            if candidate is not mount and candidate["source"].is_dir()
            and mount["target"].is_relative_to(candidate["target"])
        ]
        if not ancestors:
            continue
        ancestor = max(ancestors, key=lambda candidate: len(candidate["target"].parts))
        target = ancestor["source"] / mount["target"].relative_to(ancestor["target"])
        directory = target if mount["source"].is_dir() else target.parent
        if not directory.resolve().is_relative_to(ancestor["source"].resolve()):
            raise SystemExit(f"container mount target parent escapes mount source: {mount['target']}")
        directory.mkdir(parents=True, exist_ok=True)


def mcp_config(state_dir: Path) -> Path:
    """Write the container's MCP config, whose binaries resolve in its PATH."""
    source = json.loads((state_dir / "kitchen-mcp.json").read_text())
    for name, server in source["mcpServers"].items():
        if name == "swarm" or name.startswith("swarm-"):
            server["command"] = "swarm"
    path = state_dir / MCP_CONFIG_NAME
    path.write_text(json.dumps(source, indent=2) + "\n")
    return path


def remove(kitchen: str) -> None:
    name = f"kitchen-{kitchen}"
    owner = subprocess.run(
        ["docker", "inspect", f"--format={{{{index .Config.Labels \"{OWNER_LABEL}\"}}}}", name],
        capture_output=True, text=True,
    )
    if owner.returncode:
        stderr = owner.stderr.lower()
        if "no such container" in stderr or "no such object" in stderr:
            return
        raise subprocess.CalledProcessError(
            owner.returncode, owner.args, output=owner.stdout, stderr=owner.stderr,
        )
    if owner.stdout.strip() != kitchen:
        raise SystemExit(f'refusing to remove container "{name}": Kitchen does not own it')
    argv = ["docker", "rm", "--force", name]
    subprocess.run(argv, check=True, capture_output=True, text=True)


def run(kitchen: str, project: Path, state_dir: Path, cfg: dict,
        claude_args: list[str], env: dict[str, str]) -> None:
    name = f"kitchen-{kitchen}"
    subprocess.run(
        ["docker", "network", "inspect", cfg["network"]],
        check=True, stdout=subprocess.DEVNULL,
    )
    remove(kitchen)

    session_file = state_dir / "container-session.json"
    if not session_file.exists():
        session_file.write_text("{}\n")
    (state_dir / "notes").mkdir(exist_ok=True)
    (state_dir / "channel").mkdir(exist_ok=True)
    from claude_kitchen.state import channel_socket
    channel = channel_socket(state_dir).parent
    channel.mkdir(parents=True, exist_ok=True)
    mounts = [
        {"source": cfg["home_source"], "target": Path.home()},
        {"source": state_dir, "target": state_dir, "read_only": True},
        {"source": session_file, "target": session_file},
        {"source": state_dir / "notes", "target": state_dir / "notes"},
        {"source": channel, "target": Path("/tmp/kitchen-channel")},
    ]
    if "wiki" in cfg:
        mounts.append({"source": cfg["wiki"], "target": cfg["wiki"]})
    mounts.extend(cfg["mounts"])
    _prepare_mount_targets(mounts)
    argv = [
        "docker", "run", "--detach", "--interactive", "--tty",
        "--name", name,
        "--label", f"{OWNER_LABEL}={kitchen}",
        "--hostname", kitchen,
        "--network", cfg["network"],
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--workdir", str(project),
    ]
    container_env = {**cfg["environment"], **env}
    container_env.update(KITCHEN_CHANNEL_DIR="/tmp/kitchen-channel", KITCHEN_CONTAINER="1")
    if "wiki" in cfg:
        container_env["KITCHEN_WIKI"] = str(cfg["wiki"])
    for key, value in container_env.items():
        argv.extend(["--env", f"{key}={value}"])
    for mount in mounts:
        spec = f"type=bind,source={mount['source']},target={mount['target']}"
        if mount.get("read_only"):
            spec += ",readonly"
        argv.extend(["--mount", spec])
    argv.extend([cfg["image"], *claude_args])
    subprocess.run(argv, check=True, stdout=subprocess.DEVNULL)

    while True:
        subprocess.run(["docker", "attach", name], check=True)
        result = subprocess.run(
            ["docker", "inspect", "--format={{.State.Running}}", name],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or result.stdout.strip() != "true":
            return
