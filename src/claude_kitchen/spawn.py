"""Spawn logic for claude-kitchen agents."""
import base64
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from claude_kitchen.tmux import tmux, has_session, target, SESSION
from claude_kitchen.state import MCP_CONFIG_NAME, WINDOWS, channel_flags, pid_alive, sous_first_turn


FIRST_TURN_ENV = "KITCHEN_SOUS_FIRST_TURN"


def check_sous_pid(state_dir: Path):
    """Exit if another sous is already running for this kitchen.

    Called at the TOP of cmd_open, not from spawn_sous: a refused open must
    change nothing, and everything cmd_open does before launching the sous —
    rewriting kitchen.json and the MCP config, creating the session, and on the
    has_session branch _sweep_cooks — is a mutation."""
    pid_file = state_dir / "sous.pid"
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        return  # stale pid, fine to proceed
    # A sous whose terminal just went away (tmux kill-server, a closed window) is still exiting for a moment:
    # wait for it, up to SOUS_EXIT_WAIT. Never signal it; a live sous, or one still there after the wait, refuses.
    deadline = time.monotonic() + SOUS_EXIT_WAIT
    while True:
        if not pid_alive(pid):  # not os.kill(pid, 0): on Windows that terminates the sous
            return  # gone, fine to proceed
        if not _exiting(pid) or time.monotonic() >= deadline:
            sys.exit(f"Sous chef already running (pid {pid}). Exit it first, or kitchen close.")
        time.sleep(0.2)


SOUS_EXIT_WAIT = 15  # seconds


def _exiting(pid: int) -> bool:
    """Its terminal is gone: the sous was reparented to init (launchd, or a systemd user manager) while it exits.
    Windows reparents nothing and has no ps: there a live sous may be exiting, so it gets the whole wait
    (ponytail: a sous that really runs costs SOUS_EXIT_WAIT before the refusal)."""
    if WINDOWS:
        return True
    def ps(field, p):
        return subprocess.run(["ps", "-o", f"{field}=", "-p", str(p)], capture_output=True, text=True).stdout.strip()
    ppid = ps("ppid", pid)
    return ppid == "1" or (ppid != "" and ps("comm", ppid).endswith("systemd"))


# Exported to every agent kitchen launches (sous and cooks, any backend).
# Auto-memory is off so durable notes go to the kitchen wiki, not
# ~/.claude/projects/*/memory/. Non-Claude backends ignore it.
AGENT_ENV = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}

# `--model` names, by the backend that runs them. Claude tier aliases pass
# through verbatim to `claude --model`, which resolves the latest model in that
# tier at launch; codex names map to the full id `codex -m` accepts.
CLAUDE_MODELS = ("fable", "sonnet", "opus")
CODEX_MODELS = {"astra": "gpt-6-astra"}


STATUSLINE = Path(__file__).parent / "statusline-command.sh"


def _user_statusline() -> dict | None:
    try:
        return json.loads((Path.home() / ".claude" / "settings.json").read_text()).get("statusLine")
    except FileNotFoundError:
        return None


def spawn_sous(kitchen: str, state_dir: Path, sous_prompt: str,
               project: Path = None, slug: str = None,
               resume_session_id: str = None,
               model: str = None):
    """Replace current process with Claude as sous chef."""
    # Write our PID before exec — exec preserves the PID
    (state_dir / "sous.pid").write_text(str(os.getpid()))

    # AGENT_KITCHEN, not AGENT_SESSION: the tmux session is a constant now, so
    # the kitchen name is the only thing worth passing down and every consumer
    # (resolve_kitchen, the statusline, the hook gate) reads this.
    os.environ["AGENT_KITCHEN"] = kitchen
    os.environ["AGENT_NAME"] = "sous"
    os.environ["STATUS_DIR"] = str(state_dir)
    os.environ.update(AGENT_ENV)
    if slug:
        from claude_kitchen.state import wiki_dir, notes_dir
        os.environ["KITCHEN_WIKI"] = str(wiki_dir(slug))
        os.environ["KITCHEN_NOTES"] = str(notes_dir(kitchen))

    # --remote-control=<name> names the RC session `[sous] <kitchen>`, so in
    # the mobile session list the sous reads as a labelled entry rather than a
    # bare kitchen name sitting alongside its own `<kitchen>/<cook>` cooks
    # (build_shell_cmd names those; the CLI remote-controls all sessions by
    # default anyway — naming is the value-add).
    # The `=` form is load-bearing: the flag's arg is optional, so a separate
    # token would parse as the next flag, not the name — and this name both
    # starts with `[` and carries a space. It's one execvp argv element, so no
    # shell quoting is involved; the space never splits it.
    claude_args = [
        "claude",
        "--dangerously-skip-permissions",
        "--dangerously-load-development-channels", *channel_flags(kitchen),
        "--mcp-config", str(state_dir / MCP_CONFIG_NAME),
        f"--remote-control=[sous] {kitchen}",
    ]
    if model:
        claude_args.extend(["--model", model])
    if not _user_statusline():  # the status line is what receives the plan's usage
        claude_args.extend(["--settings", json.dumps({"statusLine": {"type": "command", "command": f"bash {shlex.quote(str(STATUSLINE))}"}})])
    if resume_session_id:
        claude_args.extend(["--resume", resume_session_id])
    claude_args.extend(["--append-system-prompt", sous_prompt])
    # `kitchen restart` builds the turn while the old sous still runs and hands it over here.
    turn = os.environ.pop(FIRST_TURN_ENV, None)
    claude_args.extend(json.loads(turn) if turn is not None else sous_first_turn(kitchen))
    if project:
        os.chdir(project)
    if WINDOWS:
        # Windows has no exec: os.execvp starts claude and exits, and the pane's
        # shell would take the console back from under it. So kitchen waits on
        # it, its pid in sous.pid, and leaves Ctrl-C to claude.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        sys.exit(subprocess.call(claude_args))
    os.execvp("claude", claude_args)


# Kitchen passes --effort through to each backend's native scale, aliasing
# only what that backend doesn't take literally. Verified 2026-07-17:
# Claude 2.1.214: low | medium | high | xhigh | max
# Codex 0.144.5 + gpt-5.6-sol/terra: low | medium | high | xhigh | max | ultra
# Codex effort support is model-dependent, so unsupported levels fail there.
_CLAUDE_EFFORT_ALIAS = {"ultra": "max"}

# Per-launch notify override appended to every codex cook. TOML array literal.
_CODEX_NOTIFY_OVERRIDE = 'notify=["kitchen","hook-codex"]'

# Appended to the inlined role for a gemini cook. agy's `-i` submits its
# argument as the first USER turn (not a system prompt), so without this agy
# acts on the role immediately — exploring/running tools instead of idling.
# This clamps it to "acknowledge and wait", the gemini analogue of cli's
# _ROLE_ACK_FOOTER for codex (which has the same role-as-first-message shape).
_GEMINI_ROLE_FOOTER = (
    "\n\n---\n"
    "The text above is your standing ROLE, not a task. Do NOT run any tools, "
    "commands, or investigation now. Reply with one short line (\"Ready, chef.\") "
    "and then wait silently — your actual ticket arrives as the next message.\n"
)


# Clean-room (eval) cooks disable the superpowers plugin so its SessionStart
# "You have superpowers" injection never fires. Passed via --settings, which
# MERGES with ~/.claude/settings.json (empirically verified) — the kitchen's
# own Stop hook there stays live, so cook→sous completion notifications still
# work. Plugin keys are marketplace-qualified, and superpowers ships from
# either marketplace below, so both are disabled.
SUPERPOWERS_MARKETPLACES = ("superpowers-marketplace", "claude-plugins-official")
_CLEAN_ROOM_SETTINGS = json.dumps(
    {"enabledPlugins": {f"superpowers@{m}": False for m in SUPERPOWERS_MARKETPLACES}})


def build_shell_cmd(backend: str, name: str, kitchen: str, status_dir: str,
                    effort: str = None, role_path: Path = None,
                    clean_room: bool = False, codex_home: str = None,
                    plugin_dirs: list = None, model: str = None, mcp_config: Path = None) -> str:
    # On Windows psmux runs the command in PowerShell, which drops double quotes there and reads curly quotes as
    # quotes too: so no value is ever PowerShell source. Each is a base64 literal the script decodes ($d), and the
    # script itself rides in -EncodedCommand. Only Claude cooks so far: cmd_hire refuses the others.
    q = (lambda v: f"(& $d '{base64.b64encode(str(v).encode()).decode()}')") if WINDOWS else shlex.quote
    env_vars = {"AGENT_NAME": name, "AGENT_KITCHEN": kitchen, "STATUS_DIR": status_dir,
                **{k: v for k, v in os.environ.items() if k.startswith("KITCHEN_")}, **AGENT_ENV}
    # Clean-room codex cooks run against a fresh, isolated CODEX_HOME (seeded in
    # cmd_hire) — no user config, memory, AGENTS.md, or plugin registry. notify
    # still rides the -c flag below, so cook→sous reporting is unaffected. Gated
    # to codex so the invariant (only codex gets CODEX_HOME) is local here.
    if codex_home and backend == "codex":
        env_vars["CODEX_HOME"] = codex_home
    env = "export " + " ".join(f"{k}={q(v)}" for k, v in env_vars.items())
    if backend == "claude":
        effort = _CLAUDE_EFFORT_ALIAS.get(effort, effort)
        effort_flag = f" --effort {q(effort)}" if effort else ""
        # Claude model selection (--model): the tier alias (fable/sonnet/opus)
        # passes through verbatim, so `claude --model` resolves the latest model
        # in that tier at launch. Omitted → empty string, so the default launch
        # command is byte-for-byte unchanged.
        model_flag = f" --model {q(model)}" if model else ""
        # Pass the file path, not the contents — the file form avoids
        # shell-quoting fragility for multi-line role prompts.
        role_flag = f" --append-system-prompt-file {q(str(role_path))}" if role_path else ""
        # Disable Claude's prompt-suggestion ghost text (the dim placeholder in
        # an empty composer): pane readers (pane_busy/peek) could otherwise
        # misread it as typed input. Claude cooks only — appended to THIS
        # branch's export, not the shared `parts` list, so it never spills onto
        # codex/gemini cooks.
        env = f"{env} CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false"
        env_vars["CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION"] = "false"
        # Clean-room (eval) cooks: disable the superpowers plugin via --settings
        # (no SessionStart injection) without touching subscription auth or the
        # kitchen Stop hook. Auto-memory is already off for every cook (AGENT_ENV).
        # The caller also passes no role_path, so clean-room cooks boot bare.
        settings_flag = f" --settings {q(_CLEAN_ROOM_SETTINGS)}" if clean_room else ""
        # Clean-room allowlist opt-in (--with-skill): each path is loaded as a
        # session-scoped plugin dir. Additive only — doesn't touch memory, the
        # disabled superpowers plugin, the role, or the sous-managed cwd. cli.py
        # gates this to clean-room claude cooks with validated paths.
        plugin_flags = "".join(f" --plugin-dir {q(p)}" for p in (plugin_dirs or []))
        # --swarm-account: the cook's own swarm server, bound to that account.
        plugin_flags += f" --mcp-config {q(str(mcp_config))}" if mcp_config else ""
        # Block AskUserQuestion: it renders an interactive picker in the cook's
        # TUI that fires no hook and blocks forever — the sous never learns of
        # it. Cooks surface questions via NEEDS_CONTEXT instead (see role prompts).
        # Name the cook's Remote Control session `<kitchen>/<cook>` so it's
        # identifiable on mobile (RC is CLI-default-on; this only names it).
        # `=` form: the flag's arg is optional, so a leading-dash name as a
        # separate token would parse as the next flag instead of the name.
        # `<kitchen>/<cook>`. This used to strip the ck- prefix off the session
        # name; the session is a constant now, so it comes straight from the
        # kitchen. Getting this wrong would silently rename every cook on the
        # head chef's phone to "kitchen/<cook>".
        rc_name = f"{kitchen}/{name}"
        rc_flag = f" {q('--remote-control=' + rc_name)}" if WINDOWS else f" --remote-control={q(rc_name)}"
        flags = f"--dangerously-skip-permissions --disallowedTools AskUserQuestion{rc_flag}{effort_flag}{model_flag}{settings_flag}{plugin_flags}{role_flag}"
        if WINDOWS:  # `exit`: the window closes with claude, as `exec` makes it on Unix
            script = ("$d = { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($args[0])) }; "
                      + "".join(f"$env:{k} = {q(v)}; " for k, v in env_vars.items()) + f"& claude.exe {flags}")
            return f"powershell.exe -NoProfile -EncodedCommand {base64.b64encode(script.encode('utf-16-le')).decode()}; exit"
        return f'bash -lc {shlex.quote(f"{env}; exec claude {flags}")}'
    elif backend == "codex":
        # Codex has no --append-system-prompt-file equivalent. Role delivery
        # happens via send_keys after wait_for_prompt (see cmd_hire).
        effort_flag = f' -c model_reasoning_effort={q(effort)}' if effort else ""
        model_flag = f" -m {q(CODEX_MODELS[model])}" if model else ""
        # Per-launch notify override. Bypasses any global notify wrapper
        # (e.g. the Codex Computer Use plugin's SkyComputerUseClient, which
        # rewrites ~/.codex/config.toml's top-level notify to wrap kitchen
        # in --previous-notify and then silently swallows the forward inside
        # workspaces enrolled with Codex Desktop). Codex CLI reads notify
        # from config at process start; -c overrides per-process without
        # touching global state. Value is a TOML array literal.
        notify_flag = f' -c {q(_CODEX_NOTIFY_OVERRIDE)}'
        return f'bash -lc {q(f"{env}; exec codex --dangerously-bypass-approvals-and-sandbox{model_flag}{effort_flag}{notify_flag}")}'
    elif backend == "gemini":
        # agy (Antigravity CLI) drives Gemini. No --append-system-prompt-file
        # equivalent, so the role is INLINED as the first interactive turn via
        # -i (shlex-quoted; a ~2.4KB role is well under the argv limit). The
        # `< /dev/null` redirect is load-bearing: agy reads stdin even with the
        # prompt on argv and hangs on launch without it. agy honors no --effort
        # flag, so effort is silently dropped (POC).
        role_flag = f" -i {q(role_path.read_text() + _GEMINI_ROLE_FOOTER)}" if role_path else ""
        return f'bash -lc {q(f"{env}; exec agy{role_flag} --dangerously-skip-permissions < /dev/null")}'
    else:
        raise ValueError(f"Unknown backend: {backend}")


def spawn_window(kitchen: str, name: str, cwd: str, backend: str, status_dir: str,
                 effort: str = None, role_path: Path = None,
                 clean_room: bool = False, codex_home: str = None,
                 plugin_dirs: list = None, model: str = None, mcp_config: Path = None) -> bool:
    """Spawn a new tmux window with an agent. Returns True on success."""
    cmd = build_shell_cmd(backend, name, kitchen, status_dir,
                          effort=effort, role_path=role_path,
                          clean_room=clean_room, codex_home=codex_home,
                          plugin_dirs=plugin_dirs, model=model, mcp_config=mcp_config)

    if has_session(kitchen):
        return _new_window(kitchen, name, cwd, cmd)
    result = tmux("new-session", "-d", "-s", SESSION, "-n", name, "-c", cwd, cmd,
                  kitchen=kitchen)
    return _launched(result)


def _new_window(kitchen: str, name: str, cwd: str, cmd: str) -> bool:
    # -d: a new window must not become current, or every attached client is
    # pulled off the window it is on.
    return _launched(tmux("new-window", "-d", "-t", target(), "-n", name, "-c", cwd,
                          cmd, kitchen=kitchen))


def _launched(result) -> bool:
    """True on success; otherwise print tmux's own reason, which the caller's
    one-line failure message would otherwise hide."""
    if result.returncode != 0:
        print(f"tmux: {result.stderr.strip()}", file=sys.stderr)
    return result.returncode == 0


