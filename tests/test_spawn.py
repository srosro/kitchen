"""Tests for spawn logic."""
import json
import os
import shlex
from pathlib import Path

import pytest
from unittest.mock import patch, MagicMock
from claude_kitchen.spawn import _runtime_hooks, build_shell_cmd, spawn_sous, spawn_window


def _codex_argv_from_shell_cmd(cmd: str) -> list[str]:
    """Reproduce what codex sees as argv after bash -lc parsing the shell
    command produced by build_shell_cmd. The outer string is
    `bash -lc '<inner>'`; bash -lc would parse <inner> through shell rules.
    shlex.split with posix=True matches that parsing for our quoting shapes."""
    outer = shlex.split(cmd)
    assert outer[:2] == ["bash", "-lc"], f"unexpected outer shape: {outer[:2]}"
    inner_tokens = shlex.split(outer[2])
    # The inner is `export ...; exec codex <args...>`. Find `exec codex` and
    # return what follows as codex's argv (program + args).
    i = inner_tokens.index("exec")
    return inner_tokens[i + 1:]


class TestBuildShellCmd:
    def test_claude_cook(self):
        cmd = build_shell_cmd(
            backend="claude", name="eng", kitchen="risotto",
            status_dir="/tmp/state",
        )
        assert "claude" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "AGENT_NAME=" in cmd
        assert "eng" in cmd

    def test_codex_cook(self):
        cmd = build_shell_cmd(
            backend="codex", name="reviewer", kitchen="risotto",
            status_dir="/tmp/state",
        )
        assert "codex" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd

    def test_codex_cook_has_notify_override(self):
        """Codex cook argv must contain `-c notify=["kitchen","hook-codex"]`
        as two adjacent tokens, surviving the bash -lc shell-quoting layer.
        Bypasses any global notify wrapper (SkyComputerUseClient et al.)."""
        cmd = build_shell_cmd(
            backend="codex", name="rev", kitchen="r",
            status_dir="/tmp/state",
        )
        argv = _codex_argv_from_shell_cmd(cmd)
        assert argv[0] == "codex"
        # Find -c notify=... in argv (there may be other -c flags too).
        notify_seen = False
        for i, tok in enumerate(argv):
            if tok == "-c" and i + 1 < len(argv) and argv[i + 1].startswith("notify="):
                assert argv[i + 1] == 'notify=["kitchen","hook-codex"]', (
                    f"notify override value malformed: {argv[i+1]!r}"
                )
                notify_seen = True
                break
        assert notify_seen, f"-c notify=... not found in codex argv: {argv}"

    def test_claude_cook_has_no_notify_override(self):
        """Claude has no equivalent notify mechanism; the override is codex-only."""
        cmd = build_shell_cmd(
            backend="claude", name="eng", kitchen="r",
            status_dir="/tmp/state",
        )
        assert "notify=" not in cmd, "notify override leaked to claude cook"

    def test_codex_notify_override_coexists_with_effort(self):
        """Effort flag and notify override must both make it through, in
        the right argv shape (each behind its own -c)."""
        cmd = build_shell_cmd(
            backend="codex", name="rev", kitchen="r",
            status_dir="/tmp/state", effort="high",
        )
        argv = _codex_argv_from_shell_cmd(cmd)
        # Two -c flags expected, in order: model_reasoning_effort, notify
        c_pairs = [
            (argv[i], argv[i + 1])
            for i in range(len(argv) - 1)
            if argv[i] == "-c"
        ]
        keys = [pair[1].split("=", 1)[0] for pair in c_pairs]
        assert "model_reasoning_effort" in keys
        assert "notify" in keys

    def test_codex_effort_values_reach_codex_natively(self):
        """Codex 0.144.5 with gpt-5.6-sol accepts literal `max` and `ultra`;
        kitchen must not collapse those distinct model-dependent tiers."""
        for effort, want in (
            ("high", "high"),
            ("xhigh", "xhigh"),
            ("max", "max"),
            ("ultra", "ultra"),
        ):
            cmd = build_shell_cmd(
                backend="codex", name="rev", kitchen="r",
                status_dir="/tmp/state", effort=effort,
            )
            argv = _codex_argv_from_shell_cmd(cmd)
            i = argv.index("-c")
            assert argv[i + 1] == f"model_reasoning_effort={want}", (
                f"--effort {effort}: {argv[i+1]!r}, want model_reasoning_effort={want}"
            )

    def test_claude_effort_ultra_aliases_to_max(self):
        """Claude 2.1.211 accepts low..xhigh|max but not `ultra`; the kitchen
        alias must land as `--effort max`. `max` itself passes verbatim."""
        for effort, want in (("max", "max"), ("ultra", "max")):
            toks = _claude_inner_tokens(build_shell_cmd(
                backend="claude", name="eng", kitchen="r",
                status_dir="/tmp/state", effort=effort,
            ))
            i = toks.index("--effort")
            assert toks[i + 1] == want, f"--effort {effort}: {toks[i+1]!r}"

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown backend"):
            build_shell_cmd(
                backend="gpt", name="eng", kitchen="risotto",
                status_dir="/tmp/state",
            )

    def test_propagates_kitchen_prefixed_env(self, monkeypatch):
        monkeypatch.setenv("KITCHEN_NOTES", "/tmp/n")
        monkeypatch.setenv("KITCHEN_WIKI", "/tmp/w")
        monkeypatch.setenv("FOO", "bar")
        cmd = build_shell_cmd(
            backend="claude", name="eng", kitchen="risotto",
            status_dir="/tmp/state",
        )
        assert "KITCHEN_NOTES=/tmp/n" in cmd
        assert "KITCHEN_WIKI=/tmp/w" in cmd
        assert "FOO=bar" not in cmd

    def test_no_kitchen_env_omits_vars(self, monkeypatch):
        for k in list(os.environ):
            if k.startswith("KITCHEN_"):
                monkeypatch.delenv(k)
        cmd = build_shell_cmd(
            backend="codex", name="eng", kitchen="risotto",
            status_dir="/tmp/state",
        )
        assert "KITCHEN_" not in cmd


def _claude_inner_tokens(cmd: str) -> list[str]:
    """Tokens of the `bash -lc '<inner>'` payload for a claude cook, parsed
    through shell rules — same approach as the codex argv helper, so assertions
    survive the shell-quoting layer instead of matching raw substrings."""
    outer = shlex.split(cmd)
    assert outer[:2] == ["bash", "-lc"], f"unexpected outer shape: {outer[:2]}"
    return shlex.split(outer[2])


class TestCleanRoom:
    def test_clean_room_disables_memory_and_superpowers_no_role(self):
        """--clean-room keeps the auto-memory env knob (before exec) AND adds the
        superpowers-disabling --settings (after exec), and passes NO role
        prompt — the three exclusions verified to give MEM=NO + SP=NO while
        subscription auth + the kitchen Stop hook stay intact."""
        cmd = build_shell_cmd(
            backend="claude", name="eval1", kitchen="r",
            status_dir="/tmp/state", clean_room=True,
        )
        toks = _claude_inner_tokens(cmd)
        # auto-memory env var is exported before `exec`
        ei = toks.index("exec")
        assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1" in toks[:ei]
        # superpowers plugin disabled via a --settings JSON arg (parsed, not substring)
        si = toks.index("--settings")
        settings = json.loads(toks[si + 1])
        assert settings["enabledPlugins"] == {
            "superpowers@superpowers-marketplace": False,
            "superpowers@claude-plugins-official": False,
        }
        # clean-room cooks boot bare — no role prompt
        assert "--append-system-prompt-file" not in toks

    def test_default_is_not_clean_room(self):
        cmd = build_shell_cmd(
            backend="claude", name="eng", kitchen="r",
            status_dir="/tmp/state",
        )
        toks = _claude_inner_tokens(cmd)
        settings = json.loads(toks[toks.index("--settings") + 1])
        assert "enabledPlugins" not in settings and settings["hooks"]["Stop"]  # its runtime hooks only

    def test_codex_clean_room_exports_codex_home_keeps_notify(self):
        """Clean-room codex cook runs against a fresh per-cook CODEX_HOME
        (exported before exec) while the -c notify override — the cook→sous
        completion path — survives. Verified empirically: notify fires under a
        fresh seeded CODEX_HOME."""
        cmd = build_shell_cmd(
            backend="codex", name="eval1", kitchen="r", status_dir="/tmp/state",
            clean_room=True, codex_home="/tmp/state/codex-home/eval1",
        )
        toks = _claude_inner_tokens(cmd)  # same bash -lc payload shape
        ei = toks.index("exec")
        # CODEX_HOME is the last export token, so shlex keeps the `;` separator on it
        assert any(t.rstrip(";") == "CODEX_HOME=/tmp/state/codex-home/eval1" for t in toks[:ei])
        # notify override still present as two adjacent argv tokens
        argv = _codex_argv_from_shell_cmd(cmd)
        assert any(
            t == "-c" and argv[i + 1].startswith("notify=")
            for i, t in enumerate(argv[:-1])
        ), f"notify override missing: {argv}"

    def test_codex_without_codex_home_omits_it(self):
        cmd = build_shell_cmd(
            backend="codex", name="rev", kitchen="r", status_dir="/tmp/state",
        )
        assert "CODEX_HOME" not in cmd

    def test_clean_room_with_plugin_dirs(self):
        """--with-skill paths arrive as repeated --plugin-dir <path> pairs on a
        clean-room claude cook, alongside (not replacing) the memory + settings
        knobs — additive opt-in to the blank slate."""
        cmd = build_shell_cmd(
            backend="claude", name="eval1", kitchen="r", status_dir="/tmp/state",
            clean_room=True, plugin_dirs=["/skills/a", "/skills/b"],
        )
        toks = _claude_inner_tokens(cmd)
        pairs = [(toks[i], toks[i + 1]) for i in range(len(toks) - 1) if toks[i] == "--plugin-dir"]
        assert ("--plugin-dir", "/skills/a") in pairs
        assert ("--plugin-dir", "/skills/b") in pairs
        assert "--settings" in toks  # blank-slate knobs still present
        assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1" in toks[:toks.index("exec")]

    def test_no_plugin_dirs_by_default(self):
        cmd = build_shell_cmd(
            backend="claude", name="eng", kitchen="r", status_dir="/tmp/state",
        )
        assert "--plugin-dir" not in cmd

    def test_codex_home_is_codex_only(self):
        """The CODEX_HOME export is gated to the codex backend inside
        build_shell_cmd — a (nonsensical) claude call with codex_home set must
        NOT leak it, so the invariant is local, not just upheld by cmd_hire."""
        cmd = build_shell_cmd(
            backend="claude", name="eng", kitchen="r", status_dir="/tmp/state",
            codex_home="/tmp/state/codex-home/eng",
        )
        assert "CODEX_HOME" not in cmd


class TestRoleInjection:
    def test_claude_role_passes_file_path(self, tmp_path):
        role_file = tmp_path / "roles" / "eng.md"
        role_file.parent.mkdir()
        role_file.write_text("# eng — implementer\nbody with 'quotes' & specials\nmultiline")
        cmd = build_shell_cmd(
            backend="claude", name="eng", kitchen="r",
            status_dir="/tmp/state", role_path=role_file,
        )
        assert "--append-system-prompt-file" in cmd
        assert str(role_file) in cmd
        # Contents must NOT be inlined — that would re-introduce the quoting risk
        assert "implementer" not in cmd
        assert "multiline" not in cmd

    def test_claude_no_role_omits_flag(self):
        cmd = build_shell_cmd(
            backend="claude", name="cook1", kitchen="r",
            status_dir="/tmp/state",
        )
        assert "--append-system-prompt-file" not in cmd


_GHOST_VAR = "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false"


def _export_segment(cmd: str) -> str:
    """The `export ...` portion of a `bash -lc 'export ...; exec ...'` command —
    i.e. the env the launched process inherits, excluding the exec'd argv."""
    outer = shlex.split(cmd)
    assert outer[:2] == ["bash", "-lc"], f"unexpected outer shape: {outer[:2]}"
    inner = outer[2]
    return inner.split("; exec", 1)[0]


class TestPromptSuggestionDisabled:
    """B2: kitchen-launched Claude COOKS disable the prompt-suggestion ghost
    text (CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false). Claude-cook-only — never
    on codex/gemini cooks, never on the sous launches."""

    def test_claude_cook_disables_ghost_text(self):
        cmd = build_shell_cmd(
            backend="claude", name="eng", kitchen="r",
            status_dir="/tmp/state",
        )
        # Present, AND in the exported env (so the exec'd claude inherits it),
        # not stray inside the argv.
        assert _GHOST_VAR in _export_segment(cmd)

    def test_codex_cook_has_no_ghost_var(self):
        cmd = build_shell_cmd(
            backend="codex", name="rev", kitchen="r",
            status_dir="/tmp/state",
        )
        assert "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION" not in cmd

    def test_gemini_cook_has_no_ghost_var(self):
        cmd = build_shell_cmd(
            backend="gemini", name="gm", kitchen="r",
            status_dir="/tmp/state",
        )
        assert "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION" not in cmd

    @patch("claude_kitchen.spawn.os.chdir")
    @patch("claude_kitchen.spawn.os.execvp")
    def test_spawn_sous_has_no_ghost_var(self, mock_exec, mock_chdir, tmp_path, monkeypatch):
        """The interactive sous is descoped from B2 — its launch argv must not
        carry the ghost-text disable."""
        for k in ("AGENT_NAME", "AGENT_SESSION", "STATUS_DIR"):
            monkeypatch.setenv(k, "")
        spawn_sous("risotto", tmp_path, "prompt text", slug="gh-x-y")
        argv = mock_exec.call_args.args[1]
        assert all("CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION" not in tok for tok in argv), (
            f"ghost-text disable leaked into sous argv: {argv}"
        )

class TestSpawnSous:
    @pytest.mark.parametrize("own", [None, {"type": "command", "command": "mine.sh"}])
    @patch("claude_kitchen.spawn.os.chdir")
    @patch("claude_kitchen.spawn.os.execvp")
    def test_the_sous_runs_kitchens_statusline_unless_the_user_has_one(self, mock_exec, mock_chdir,
                                                                        tmp_path, monkeypatch, own):
        """The statusline is what receives the plan's usage: with none of the user's
        own, the sous runs kitchen's, which keeps it; theirs stays theirs."""
        monkeypatch.setenv("HOME", str(tmp_path))
        if own:
            (tmp_path / ".claude").mkdir()
            (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"statusLine": own}))
        spawn_sous("risotto", tmp_path, "prompt text", slug="gh-x-y")
        argv = mock_exec.call_args.args[1]
        settings = [json.loads(argv[i + 1]) for i, a in enumerate(argv) if a == "--settings"]
        [line] = settings
        assert line["hooks"]["Stop"]  # kitchen's runtime hooks ride on the sous, never the user's settings
        if own:
            assert "statusLine" not in line
        else:
            assert line["statusLine"]["command"].endswith("statusline-command.sh")

    @patch("claude_kitchen.spawn.os.chdir")
    @patch("claude_kitchen.spawn.os.execvp")
    def test_chdir_to_project(self, mock_exec, mock_chdir, tmp_path, monkeypatch):
        for k in ("AGENT_NAME", "AGENT_SESSION", "STATUS_DIR"):
            monkeypatch.setenv(k, "")
        spawn_sous("risotto", tmp_path, "prompt text", project=Path("/tmp/myproject"), slug="gh-x-y")
        mock_chdir.assert_called_once_with(Path("/tmp/myproject"))

    @patch("claude_kitchen.spawn.os.chdir")
    @patch("claude_kitchen.spawn.os.execvp")
    def test_no_chdir_when_no_project(self, mock_exec, mock_chdir, tmp_path, monkeypatch):
        for k in ("AGENT_NAME", "AGENT_SESSION", "STATUS_DIR"):
            monkeypatch.setenv(k, "")
        spawn_sous("risotto", tmp_path, "prompt text", slug="gh-x-y")
        mock_chdir.assert_not_called()

    @patch("claude_kitchen.spawn.os.chdir")
    @patch("claude_kitchen.spawn.os.execvp")
    def test_remote_control_named_with_kitchen(
        self, mock_exec, mock_chdir, tmp_path, monkeypatch,
    ):
        """Sous launches with --remote-control=[sous] <kitchen> as ONE argv
        token — the `[sous] ` label is what distinguishes it from its own
        `<kitchen>/<cook>` cooks in the mobile session list. The `=` form is
        what keeps a name that leads with `[` (and carries a space, and may
        lead with a dash) bound as the value rather than parsed as a flag."""
        for k in ("AGENT_NAME", "AGENT_SESSION", "STATUS_DIR"):
            monkeypatch.setenv(k, "")
        spawn_sous("risotto", tmp_path, "prompt", slug="gh-x-y")
        argv = mock_exec.call_args.args[1]
        assert "--remote-control=[sous] risotto" in argv
        # The space must not have split the name across two argv elements.
        assert "[sous]" not in argv and "risotto" not in argv
        assert not any(a.startswith("--remote-control-session") for a in argv)
        (tmp_path / "sous.pid").unlink()  # re-spawn under the same state dir
        spawn_sous("-x", tmp_path, "prompt", slug="gh-x-y")
        assert "--remote-control=[sous] -x" in mock_exec.call_args.args[1]

    @patch("claude_kitchen.spawn.os.chdir")
    @patch("claude_kitchen.spawn.os.execvp")
    def test_model_omitted_by_default_and_overrides(
        self, mock_exec, mock_chdir, tmp_path, monkeypatch,
    ):
        """No model → no --model at all, so the sous runs the account default;
        an explicit tier goes to `claude --model` verbatim."""
        for k in ("AGENT_NAME", "AGENT_SESSION", "STATUS_DIR"):
            monkeypatch.setenv(k, "")
        spawn_sous("risotto", tmp_path, "prompt", slug="gh-x-y")
        assert "--model" not in mock_exec.call_args.args[1]
        (tmp_path / "sous.pid").unlink()
        spawn_sous("risotto", tmp_path, "prompt", slug="gh-x-y", model="opus")
        argv = mock_exec.call_args.args[1]
        assert argv[argv.index("--model") + 1] == "opus"

    def test_cook_remote_control_naming(self):
        """Claude cooks get a named `<kitchen>/<cook>` RC session (ck- prefix
        stripped) that survives shell parsing as ONE token — even for names
        full of quotes/semicolons/substitution — proving the shlex.quote layer
        holds. Codex/gemini cooks get no RC flag."""
        nasty = "e\"n'g;$(boom) x"
        for name, want in (
            ("eng", "--remote-control=r/eng"),
            ("-x", "--remote-control=r/-x"),          # leading dash binds via `=`
            (nasty, f"--remote-control=r/{nasty}"),   # injection stays one token
        ):
            toks = _claude_inner_tokens(build_shell_cmd(
                backend="claude", name=name, kitchen="r",
                status_dir="/tmp/state",
            ))
            assert toks.count(want) == 1, f"{name!r} → {toks}"
        for backend in ("codex", "gemini"):
            cmd = build_shell_cmd(
                backend=backend, name="eng", kitchen="r",
                status_dir="/tmp/state",
            )
            assert "--remote-control" not in cmd, f"{backend} cook leaked RC flag"

    @patch("claude_kitchen.spawn.os.chdir")
    @patch("claude_kitchen.spawn.os.execvp")
    def test_exports_wiki_and_notes_env(self, mock_exec, mock_chdir, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("KITCHEN_WIKI", raising=False)
        monkeypatch.delenv("KITCHEN_NOTES", raising=False)
        for k in ("AGENT_NAME", "AGENT_SESSION", "STATUS_DIR"):
            monkeypatch.setenv(k, "")
        state = tmp_path / ".claude-kitchen" / "risotto"
        state.mkdir(parents=True)
        spawn_sous(
            "risotto", state,
            "prompt", project=Path("/tmp/p"), slug="gh-acme-widget",
        )
        import os
        assert os.environ["KITCHEN_WIKI"] == str(
            tmp_path / ".claude-kitchen" / "projects" / "gh-acme-widget" / "wiki"
        )
        assert os.environ["KITCHEN_NOTES"] == str(
            tmp_path / ".claude-kitchen" / "risotto" / "notes"
        )
        # Register post-spawn values with monkeypatch so its teardown restores them
        monkeypatch.setenv("KITCHEN_WIKI", os.environ["KITCHEN_WIKI"])
        monkeypatch.setenv("KITCHEN_NOTES", os.environ["KITCHEN_NOTES"])


class TestAutoMemoryDisabled:
    """Every agent kitchen launches runs with Claude Code auto-memory off, so
    durable notes land in the kitchen wiki — the sous and every cook."""

    VAR = "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1"

    def _exported(self, cmd: str) -> list[str]:
        toks = _claude_inner_tokens(cmd)
        return [t.rstrip(";") for t in toks[:toks.index("exec")]]

    @pytest.mark.parametrize("backend", ["claude", "codex", "gemini"])
    def test_cook(self, backend):
        cmd = build_shell_cmd(backend=backend, name="eng", kitchen="r",
                              status_dir="/tmp/state")
        assert self.VAR in self._exported(cmd)

    def test_root_sous(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", raising=False)
        for k in ("AGENT_NAME", "AGENT_KITCHEN", "STATUS_DIR"):
            monkeypatch.setenv(k, "")
        seen = {}
        with patch("claude_kitchen.spawn.os.execvp",
                   side_effect=lambda *a: seen.update(os.environ)):
            spawn_sous("risotto", tmp_path, "prompt")
        assert seen["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        monkeypatch.setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")  # teardown restores


class TestKitchenIdentityIsNotTheSessionName:
    """The session name is the constant "kitchen" now, so it cannot carry
    kitchen identity. Everything that used to derive identity from it must read
    the kitchen directly — pinned here because both failures are SILENT."""

    def test_remote_control_name_is_kitchen_slash_cook(self):
        # PR #14: cooks show as "<kitchen>/<cook>" on the head chef's phone.
        # Deriving this from the session name would rename every cook in every
        # kitchen to "kitchen/<cook>" and quietly undo that feature.
        cmd = build_shell_cmd(backend="claude", name="eng",
                              kitchen="domo-source", status_dir="/tmp/s")
        assert "--remote-control=domo-source/eng" in cmd
        assert "--remote-control=kitchen/eng" not in cmd

    def test_cook_env_carries_the_kitchen_not_the_session(self):
        cmd = build_shell_cmd(backend="claude", name="eng",
                              kitchen="domo-source", status_dir="/tmp/s")
        assert "AGENT_KITCHEN=domo-source" in cmd
        assert "AGENT_SESSION" not in cmd

    def test_window_targets_use_the_static_session(self):
        from claude_kitchen.tmux import target, SESSION
        assert SESSION == "kitchen"
        assert target("eng") == "kitchen:eng"
        assert target() == "kitchen:"


class TestSpawnWindow:
    def test_hires_beside_a_cook_named_after_the_session(self, tmp_path):
        """A cook called `kitchen` shares the session's name; a bare
        `new-window -t kitchen` resolved to that window and every later hire
        failed with "index N in use". The hire must also leave the current
        window alone, or attached clients get pulled onto the new cook. Real
        tmux, on a throwaway socket."""
        from claude_kitchen.tmux import SESSION, tmux
        kitchen = f"hire-target-{os.getpid()}"
        tmux("new-session", "-d", "-s", SESSION, "-n", "kitchen", "sleep 30", kitchen=kitchen, check=True)
        sock = Path(tmux("display", "-p", "#{socket_path}", kitchen=kitchen).stdout.strip())
        try:
            with patch("claude_kitchen.spawn.build_shell_cmd", return_value="sleep 30"):
                assert spawn_window(kitchen, "rev1", str(tmp_path), "claude", str(tmp_path))
            windows = tmux("list-windows", "-t", f"{SESSION}:", "-F", "#{window_name}:#{window_active}", kitchen=kitchen).stdout.split()
            assert windows == ["kitchen:1", "rev1:0"]
        finally:
            tmux("kill-server", kitchen=kitchen)
            sock.unlink(missing_ok=True)  # tmux leaves the socket file behind

    @patch("claude_kitchen.spawn.has_session", return_value=True)
    @patch("claude_kitchen.spawn.tmux")
    def test_failed_launch_prints_tmux_reason(self, mock_tmux, _has, tmp_path, capsys):
        mock_tmux.return_value = MagicMock(returncode=1, stderr="create window failed: index 2 in use\n")
        assert not spawn_window("k", "rev1", str(tmp_path), "claude", str(tmp_path))
        assert "tmux: create window failed: index 2 in use" in capsys.readouterr().err


class TestModelSelection:
    """--model passes the tier alias through to `claude --model <alias>` verbatim
    (the CLI resolves the latest model in that tier); omitting it leaves the
    launch command byte-for-byte at today's default."""

    def test_model_alias_passed_through(self):
        for alias in ("fable", "sonnet", "opus"):
            cmd = build_shell_cmd(
                backend="claude", name="eng", kitchen="r",
                status_dir="/tmp/state", model=alias,
            )
            toks = _claude_inner_tokens(cmd)
            i = toks.index("--model")
            assert toks[i + 1] == alias, f"{alias} → {toks[i+1]!r}, want the bare alias {alias!r}"

    def test_model_omitted_is_golden_default(self, monkeypatch):
        """No --model → the claude launch argv is byte-for-byte the golden
        default. KITCHEN_ env cleared so the export prefix is deterministic;
        we assert the EXACT exec argv, not just the absence of a substring."""
        for k in list(os.environ):
            if k.startswith("KITCHEN_"):
                monkeypatch.delenv(k, raising=False)
        default = build_shell_cmd(
            backend="claude", name="eng", kitchen="r", status_dir="/tmp/state",
        )
        toks = _claude_inner_tokens(default)
        exec_argv = toks[toks.index("exec") + 1:]
        assert exec_argv == [
            "claude",
            "--dangerously-skip-permissions",
            "--disallowedTools", "AskUserQuestion",
            "--remote-control=r/eng",
            "--settings", json.dumps({"hooks": _runtime_hooks()}),
        ]
        # And model=None is byte-for-byte identical to omitting the kwarg.
        assert default == build_shell_cmd(
            backend="claude", name="eng", kitchen="r", status_dir="/tmp/state",
            model=None,
        )

    def test_codex_astra_maps_to_full_id_and_omitted_is_default(self):
        """--model astra reaches codex as `-m gpt-6-astra` (the id codex
        accepts; bare `astra` is rejected server-side). No model → no -m, so
        codex runs its own default."""
        argv = _codex_argv_from_shell_cmd(build_shell_cmd(
            backend="codex", name="rev", kitchen="r",
            status_dir="/tmp/state", model="astra",
        ))
        assert argv[argv.index("-m") + 1] == "gpt-6-astra"
        argv = _codex_argv_from_shell_cmd(build_shell_cmd(
            backend="codex", name="rev", kitchen="r", status_dir="/tmp/state",
        ))
        assert "-m" not in argv

    def test_effort_survives_every_model_combination(self):
        """--effort lands on both backends, with and without --model."""
        for backend, model in (("claude", None), ("claude", "opus"),
                               ("codex", None), ("codex", "astra")):
            cmd = build_shell_cmd(
                backend=backend, name="c", kitchen="r",
                status_dir="/tmp/state", effort="ultra", model=model,
            )
            if backend == "claude":
                toks = _claude_inner_tokens(cmd)
                assert toks[toks.index("--effort") + 1] == "max", cmd
                assert ("--model" in toks) == bool(model), cmd
            else:
                argv = _codex_argv_from_shell_cmd(cmd)
                assert "model_reasoning_effort=ultra" in argv, cmd
                assert ("-m" in argv) == bool(model), cmd

class TestCheckSousPid:
    """`kitchen open` right after the sous's terminal went away (tmux kill-server: Jeff's restart steps) must wait for
    the old sous to finish exiting, never signal it, and still refuse a sous that is alive."""

    @staticmethod
    def _orphan(seconds):
        """A process whose parent already exited (as a sous is once its tmux is killed); init reaps it when it ends."""
        import subprocess
        out = subprocess.run(["sh", "-c", f"sleep {seconds} >/dev/null 2>&1 & echo $!"], capture_output=True, text=True)
        return int(out.stdout.strip())

    def test_an_exiting_sous_is_waited_for(self, tmp_path, monkeypatch):
        import time
        from claude_kitchen import spawn
        pid = self._orphan(1)
        (tmp_path / "sous.pid").write_text(str(pid))
        monkeypatch.setattr(spawn, "_exiting", lambda p: True)
        t0 = time.monotonic()
        spawn.check_sous_pid(tmp_path)  # returns once it is gone: no SystemExit
        assert time.monotonic() - t0 < spawn.SOUS_EXIT_WAIT
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    def test_it_never_signals_the_sous_and_refuses_after_the_wait(self, tmp_path, monkeypatch):
        from claude_kitchen import spawn
        pid = self._orphan(30)
        try:
            (tmp_path / "sous.pid").write_text(str(pid))
            monkeypatch.setattr(spawn, "_exiting", lambda p: True)
            monkeypatch.setattr(spawn, "SOUS_EXIT_WAIT", 0.5)
            with pytest.raises(SystemExit, match="already running"):
                spawn.check_sous_pid(tmp_path)
            os.kill(pid, 0)  # still alive: waiting never kills it
        finally:
            os.kill(pid, 9)

    @pytest.mark.parametrize("pid", [os.getpid(), 1], ids=["kitchen-itself", "another-users-process"])
    def test_a_pid_reused_after_the_sous_is_gone_is_no_sous(self, tmp_path, pid):
        """A container restart hands the old sous's pid to a new process, often kitchen's own open."""
        import subprocess, sys
        from claude_kitchen import spawn
        env = {**os.environ, "AGENT_NAME": "", "AGENT_SESSION": "", "STATUS_DIR": ""}
        subprocess.run([sys.executable, "-c",
                        "import sys; from pathlib import Path; from unittest.mock import patch; "
                        "from claude_kitchen.spawn import spawn_sous; "
                        "patch('os.execvp').start(); patch('os.chdir').start(); "
                        "spawn_sous('k', Path(sys.argv[1]), 'p', slug='gh-x-y')", str(tmp_path)],
                       env=env, check=True)
        assert len((tmp_path / "sous.pid").read_text().split(maxsplit=1)) == 2, "no start time recorded"
        # The sous exited and its pid now belongs to a process started at another time.
        (tmp_path / "sous.pid").write_text(f"{pid} Thu Jan  1 00:00:00 1970")
        spawn.check_sous_pid(tmp_path)  # no SystemExit, no PermissionError

    def test_a_live_sous_is_refused_at_once(self, tmp_path):
        import time
        from claude_kitchen import spawn
        from claude_kitchen.state import process_started
        (tmp_path / "sous.pid").write_text(f"{os.getpid()} {process_started(os.getpid())}")  # alive, with its parent: not exiting
        t0 = time.monotonic()
        with pytest.raises(SystemExit, match="already running"):
            spawn.check_sous_pid(tmp_path)
        assert time.monotonic() - t0 < 1
def test_windows_claude_cook_never_puts_a_value_in_powershell_source(monkeypatch):
    """psmux runs a window's command in PowerShell, which drops double quotes there and reads curly quotes
    (U+2018-201B) as quotes: every value rides base64-encoded inside an -EncodedCommand script."""
    import base64, re
    import claude_kitchen.spawn as spawn
    monkeypatch.setattr(spawn, "WINDOWS", True)
    tricky = "o'b\u2019r\u201c$(x)\"k"
    cmd = build_shell_cmd("claude", "eng", tricky, r"C:\Users\me\.claude-kitchen\k", effort="high",
                          role_path=Path(r"C:\roles\eng.md"))
    m = re.fullmatch(r"powershell\.exe -NoProfile -EncodedCommand ([A-Za-z0-9+/=]+); exit", cmd)
    assert m, cmd
    script = base64.b64decode(m.group(1)).decode("utf-16-le")
    assert "\u2019" not in script and "$(x)" not in script
    values = [base64.b64decode(v).decode() for v in re.findall(r"'([A-Za-z0-9+/=]*)'", script)]
    assert tricky in values and f"--remote-control={tricky}/eng" in values and "high" in values
    assert r"C:\roles\eng.md" in values
    assert "& claude.exe --dangerously-skip-permissions --disallowedTools AskUserQuestion" in script


def test_windows_kitchen_open_never_signals_a_live_sous(tmp_path, monkeypatch):
    """On Windows os.kill(pid, 0) terminates the process: the live-sous check must only look (pid_alive)."""
    import claude_kitchen.spawn as spawn
    import claude_kitchen.state as state
    for mod in (spawn, state):
        monkeypatch.setattr(mod, "WINDOWS", True)
        monkeypatch.setattr(mod, "pid_alive", lambda pid: True)
    monkeypatch.setattr(spawn, "SOUS_EXIT_WAIT", 0.3)
    monkeypatch.setattr(spawn.os, "kill", lambda *a: pytest.fail(f"signalled the sous: os.kill{a}"))
    (tmp_path / "sous.pid").write_text("4242")
    with pytest.raises(SystemExit, match="already running"):
        spawn.check_sous_pid(tmp_path)
