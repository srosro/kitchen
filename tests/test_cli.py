"""Tests for the kitchen CLI."""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import httpx
import pytest

from claude_kitchen.cli import resolve_kitchen, resolve_project, cmd_brigade, cmd_hook, cmd_open, cmd_hire, cmd_close, cmd_clock_out, _codex_rollout, _sweep_cooks, cmd_sweep, main, _agy_summary, SKIP_PRIORITY_WARNING
from claude_kitchen.state import channel_socket, read_status, update_status, write_status
from claude_kitchen.tmux import CK_PREFIX, PROBE_TIMEOUT, SESSION, PromptBlocked


class TestResolveKitchen:
    @patch("claude_kitchen.cli.list_kitchens", return_value=["risotto"])
    def test_explicit_flag(self, mock_ls):
        assert resolve_kitchen(kitchen="risotto") == "risotto"

    @patch("claude_kitchen.cli.list_kitchens", return_value=["risotto"])
    def test_from_env(self, mock_ls, monkeypatch):
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        assert resolve_kitchen() == "risotto"

    @patch("claude_kitchen.cli.list_kitchens", return_value=["risotto"])
    def test_single_kitchen(self, mock_ls, monkeypatch):
        monkeypatch.delenv("AGENT_KITCHEN", raising=False)
        assert resolve_kitchen() == "risotto"

    @patch("claude_kitchen.cli.list_kitchens", return_value=["a", "b"])
    def test_ambiguous_raises(self, mock_ls, monkeypatch):
        monkeypatch.delenv("AGENT_KITCHEN", raising=False)
        with pytest.raises(SystemExit):
            resolve_kitchen()

    def test_rejects_reserved_projects_name(self):
        with pytest.raises(SystemExit, match="reserved"):
            resolve_kitchen(kitchen="projects")


class TestResolveProject:
    def test_existing_directory_resolves(self, tmp_path):
        assert resolve_project(str(tmp_path)) == tmp_path.resolve()

    def test_bare_nonexistent_name_fails_clearly(self):
        with pytest.raises(SystemExit, match="does not resolve to a directory"):
            resolve_project("this-name-is-not-a-real-path-xyz")


def _stdin_payload(monkeypatch, **fields):
    """Fabricate a Claude Code Stop hook stdin payload matching the real shape
    captured against v2.1.112: includes last_assistant_message directly."""
    monkeypatch.setattr(
        "sys.stdin",
        MagicMock(read=MagicMock(return_value=json.dumps(fields))),
    )


def _codex_notify_payload(**fields):
    """Fabricate a Codex notify argv payload matching the real shape captured
    against Codex CLI v0.125.0: hyphenated keys, JSON passed as argv[1]."""
    payload = {
        "type": "agent-turn-complete",
        "thread-id": "00000000-0000-0000-0000-000000000000",
        "turn-id": "00000000-0000-0000-0000-000000000000",
        "cwd": "/repo",
        "client": "codex_exec",
        "input-messages": ["<redacted user prompt>"],
        "last-assistant-message": "<redacted assistant response>",
    }
    payload.update(fields)
    return json.dumps(payload)


class TestHook:
    def test_stop_writes_status_and_sends_to_socket(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        _stdin_payload(
            monkeypatch,
            hook_event_name="Stop",
            last_assistant_message="all tests pass",
            session_id="abc123",
        )

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        status_file = tmp_path / "cooks" / "eng.json"
        data = json.loads(status_file.read_text())
        assert data["status"] == "idle"
        assert data["summary"] == "all tests pass"

        mock_send.assert_called_once()
        call_args = mock_send.call_args
        assert call_args[0][0] == channel_socket(tmp_path)
        assert call_args[0][1]["cook"] == "eng"
        assert call_args[0][1]["summary"] == "all tests pass"

    @pytest.mark.parametrize("owes_a_chat", [False, True])
    def test_a_silent_turn_end_wakes_no_one_unless_it_owes_a_chat(self, monkeypatch, tmp_path, owes_a_chat):
        monkeypatch.setenv("AGENT_NAME", "relay")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        if owes_a_chat:
            update_status(tmp_path, "relay", chat_message={"message": "swarm !room $evt"})
        _stdin_payload(monkeypatch, hook_event_name="Stop",
                       last_assistant_message="Nothing to relay.\n\nSILENT\n")

        pushes = []
        with patch("claude_kitchen.channel.send_to_socket", lambda _sock, push: pushes.append(push)):
            cmd_hook(argparse.Namespace(command="hook"))

        status = read_status(tmp_path, "relay")
        assert status["status"] == "idle" and "chat_message" not in status
        assert [p.get("chat_message") for p in pushes] == (["swarm !room $evt"] if owes_a_chat else [])

    def _dead_login(self, monkeypatch, tmp_path, event="StopFailure", line=False, swarm_exit=0,
                    error="authentication_failed"):
        """`kitchen hook` on `event` in kitchen risotto, whose account lives in its own
        SWARM_HOME, with a fake `swarm` that records its argv, SWARM_HOME and stdin and
        exits `swarm_exit`. Returns the owner's Plow chat texts and the hook input."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "swarm").write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/argv\necho "$SWARM_HOME" >> {tmp_path}/home\n'
                                      f'cat >> {tmp_path}/stdin\necho "error: unrecognized subcommand" >&2\nexit {swarm_exit}\n')
        (bin_dir / "swarm").chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("SWARM_HOME", raising=False)
        account = tmp_path / "swarm-home" / "accounts" / "s.test" / "oak"
        account.mkdir(parents=True)
        (account / "credential.json").write_text("{}")
        kitchen = tmp_path / ".claude-kitchen" / "risotto"
        (kitchen / "swarm").mkdir(parents=True)
        (kitchen / "swarm" / "credential.json").symlink_to(account / "credential.json")
        if line:
            (kitchen / "plow-chat").mkdir()
            (kitchen / "plow-chat" / "plow-credentials").write_text("t")
        texts = []
        monkeypatch.setattr("claude_kitchen.plow_chat.tell_owner", lambda _k, text: texts.append(text))
        monkeypatch.setenv("AGENT_NAME", "sous")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(kitchen))
        said = {"authentication_failed": "Login expired · Please run /login",
                "rate_limit": "You've hit your weekly limit · resets Nov 20 at 2am (Europe/Berlin)"}[error]
        payload = {"hook_event_name": event, "error": error, "session_id": "s1", "last_assistant_message": said}
        _stdin_payload(monkeypatch, **payload)
        cmd_hook(argparse.Namespace(command="hook"))
        return texts, payload

    @pytest.mark.parametrize("event,error,line,told", [
        ("StopFailure", "authentication_failed", False, "swarm"),
        ("StopFailure", "authentication_failed", True, "line"),
        ("StopFailure", "rate_limit", True, "swarm"),
        ("StopFailure", "rate_limit", False, "swarm"),
        ("Stop", "authentication_failed", False, None)])
    def test_a_turn_dead_on_its_login_tells_the_owner_once_on_the_line_else_on_swarm(
            self, monkeypatch, tmp_path, event, error, line, told):
        """The dead session can't speak: a kitchen with a Plow chat line texts the owner
        an expired login there (#99), the only failure the line texts; every other failure,
        a usage limit or no line, goes to `swarm login-hook`, with the SWARM_HOME its account
        lives in, since a kitchen has no swarm plugin hook. A turn that ended well tells no one."""
        texts, payload = self._dead_login(monkeypatch, tmp_path, event, line, error=error)
        assert len(texts) == (told == "line")
        if told == "swarm":
            assert (tmp_path / "argv").read_text() == "login-hook\n"
            assert (tmp_path / "home").read_text() == f"{tmp_path / 'swarm-home'}\n"
            assert json.loads((tmp_path / "stdin").read_text()) == payload
        else:
            assert not (tmp_path / "argv").exists()

    def test_a_usage_limit_shows_paused_until_its_reset_in_brigade_until_a_turn_ends_well(
            self, monkeypatch, tmp_path, capsys):
        """The sous's turn dies on the plan's usage limit (the line Claude prints): brigade
        shows it paused with the reset time (the sous has no window, and sweep keeps its record),
        and its next turn that ends well clears it."""
        self._dead_login(monkeypatch, tmp_path, error="rate_limit")
        base = tmp_path / ".claude-kitchen" / "risotto"
        assert read_status(base, "sous")["resets"] == "Nov 20 at 2am (Europe/Berlin)"
        with patch("claude_kitchen.cli.list_kitchens", return_value=["risotto"]), \
                patch("claude_kitchen.cli.list_windows", return_value=[]):  # the sous has no window
            assert _sweep_cooks(base, "risotto") == []
            cmd_brigade(kitchen=None)
            assert "  sous  paused   —  resets Nov 20 at 2am (Europe/Berlin)" in capsys.readouterr().out
            _stdin_payload(monkeypatch, hook_event_name="Stop", session_id="s1")
            cmd_hook(argparse.Namespace(command="hook"))
            cmd_brigade(kitchen=None)
            assert capsys.readouterr().out.splitlines()[1].split() == ["sous", "idle", "—"]

    def _turn_ends_at(self, monkeypatch, tmp_path, week, sous_listening=True):
        """A cook's turn ends with the plan's weekly window at `week` (None: nothing
        kept); returns what reached the sous from kitchen itself."""
        usage = tmp_path / ".claude" / "usage" / "rate_limits.json"
        usage.parent.mkdir(parents=True, exist_ok=True)
        if week:
            usage.write_text(json.dumps({"updated_at": "2026-11-17T21:00:00Z", "seven_day": week}))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path / "risotto"))
        _stdin_payload(monkeypatch, hook_event_name="Stop", last_assistant_message="done")
        pushes = []

        def deliver(_sock, push):
            if not sous_listening:
                raise ConnectionRefusedError
            pushes.append(push)
        with patch("claude_kitchen.channel.deliver_to_socket", deliver):
            cmd_hook(argparse.Namespace(command="hook"))
        return [p["summary"] for p in pushes if p["cook"] == "kitchen"]

    def test_weekly_usage_past_80_then_95_asks_the_sous_to_tell_the_owner_once_each(
            self, monkeypatch, tmp_path, capsys):
        """Turns end as the week fills: one notice past 80%, one past 95%, none again in
        the same window, and one missed while no sous listens is told on a later turn;
        a new window warns afresh. Brigade shows the week's usage."""
        resets = int((datetime.now() + timedelta(days=2)).timestamp())
        assert self._turn_ends_at(monkeypatch, tmp_path, {"used_percentage": 82, "resets_at": resets},
                                  sous_listening=False) == []
        told = [self._turn_ends_at(monkeypatch, tmp_path, {"used_percentage": pct, "resets_at": resets})
                for pct in (50, 83, 85, 96, 97)]
        assert [len(t) for t in told] == [0, 1, 0, 1, 0]
        assert told[1][0].startswith("Claude weekly usage is at 83%; it resets ")
        assert "Tell your owner once" in told[3][0] and "96%" in told[3][0]
        later = {"used_percentage": 81, "resets_at": resets + 7 * 86400}
        assert len(self._turn_ends_at(monkeypatch, tmp_path, later)) == 1
        (tmp_path / ".claude-kitchen" / "risotto").mkdir(parents=True)
        with patch("claude_kitchen.cli.list_kitchens", return_value=["risotto"]), \
                patch("claude_kitchen.cli.list_windows", return_value=[]):
            cmd_brigade(kitchen=None)
        assert "kitchen: risotto  (Claude week 81%)" in capsys.readouterr().out

    @pytest.mark.parametrize("week", [None, {"used_percentage": 97, "resets_at": 1763629200},
                                      {"used_percentage": 97}])
    def test_no_or_stale_usage_warns_no_one(self, monkeypatch, tmp_path, week):
        """Nothing kept, a window whose reset has passed, or one with no reset time: no notice."""
        assert self._turn_ends_at(monkeypatch, tmp_path, week) == []

    def test_a_swarm_without_login_hook_stays_quiet(self, monkeypatch, tmp_path, capfd):
        """swarm 158 has no login-hook: it exits 2 with a usage error, which must not
        show on every dead login."""
        self._dead_login(monkeypatch, tmp_path, swarm_exit=2)
        assert (tmp_path / "argv").read_text() == "login-hook\n"
        assert capfd.readouterr() == ("", "")

    def test_a_claude_p_run_inside_a_cook_does_not_report_as_it(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
        update_status(tmp_path, "eng", status="working", session_id="cook-session")
        _stdin_payload(monkeypatch, hook_event_name="Stop", last_assistant_message="pong",
                       session_id="nested-session")

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        mock_send.assert_not_called()
        assert read_status(tmp_path, "eng") == {"status": "working", "session_id": "cook-session"}

    def test_sous_stop_is_noop(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_NAME", "sous")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        # Assemble the legacy sentinel at runtime — keeps the residue grep
        # in Task 11 Step 1 clean.
        sentinel = f"[{'AWAITING'}: head-chef decision on rollout]"
        _stdin_payload(
            monkeypatch,
            hook_event_name="Stop",
            last_assistant_message=f"Phase one done.\n\n{sentinel}",
        )

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        # No cook status file written.
        assert not (tmp_path / "cooks").exists() or not any((tmp_path / "cooks").iterdir())
        # No channel push.
        mock_send.assert_not_called()
        # No marker file written under notes/ (covers the old awaiting-sentinel file).
        notes = tmp_path / "notes"
        assert not notes.exists() or not any(notes.iterdir())

    def test_hook_noop_outside_kitchen(self, monkeypatch):
        monkeypatch.delenv("AGENT_NAME", raising=False)
        monkeypatch.delenv("AGENT_KITCHEN", raising=False)
        monkeypatch.delenv("STATUS_DIR", raising=False)
        # Should return without error
        cmd_hook(argparse.Namespace(command="hook"))

    def test_user_prompt_submit_writes_working_and_does_not_send(self, monkeypatch, tmp_path):
        """UserPromptSubmit is the canonical 'cook started working' trigger
        for Claude cooks. Must write status='working' and must NOT push to
        the channel socket (it's not a completion event)."""
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "hello"})
        monkeypatch.setattr("sys.stdin", MagicMock(read=MagicMock(return_value=payload)))

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["status"] == "working"
        assert data["agent"] == "eng"
        mock_send.assert_not_called()

    def test_hook_ignores_unknown_event_types(self, monkeypatch, tmp_path):
        """Events other than UserPromptSubmit / Stop are ignored — no status
        write, no socket push."""
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash"})
        monkeypatch.setattr("sys.stdin", MagicMock(read=MagicMock(return_value=payload)))

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        assert not (tmp_path / "cooks" / "eng.json").exists()
        mock_send.assert_not_called()

    def test_hook_silent_on_bad_json(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        monkeypatch.setattr("sys.stdin", MagicMock(read=MagicMock(return_value="not json{")))

        # Should not raise
        cmd_hook(argparse.Namespace(command="hook"))

    def test_codex_notify_writes_status_and_sends_to_socket(self, monkeypatch, tmp_path):
        _stage_codex_rollout(tmp_path, "00000000-0000-0000-0000-000000000000", info=None)
        monkeypatch.setattr("claude_kitchen.cli.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.setenv("AGENT_NAME", "codex-eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        payload = _codex_notify_payload(
            **{"last-assistant-message": "captured codex summary"}
        )

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=payload))

        status_file = tmp_path / "cooks" / "codex-eng.json"
        data = json.loads(status_file.read_text())
        assert data["status"] == "idle"
        assert data["summary"] == "captured codex summary"
        assert data["session_id"] == "00000000-0000-0000-0000-000000000000"

        mock_send.assert_called_once()
        call_args = mock_send.call_args
        assert call_args[0][0] == channel_socket(tmp_path)
        assert call_args[0][1]["cook"] == "codex-eng"
        assert call_args[0][1]["summary"] == "captured codex summary"


def _stage_transcript(tmp_path, *, model, usage):
    """Write a transcript JSONL with one assistant line, structurally
    faithful to the shape captured 2026-04-29 against Claude Code v2.1.119
    (transcript at ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl).
    Returns the path to feed into payload['transcript_path']."""
    line = {
        "type": "assistant",
        "uuid": "00000000-0000-0000-0000-000000000001",
        "sessionId": "00000000-0000-0000-0000-000000000000",
        "version": "2.1.119",
        "message": {
            "model": model,
            "id": "msg_redacted",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "<redacted>"}],
            "usage": usage,
        },
    }
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps(line) + "\n")
    return str(transcript)


class TestClaudeStopTokenCapture:
    """Stop hook reads usage from the transcript JSONL the payload points
    at and writes `tokens: {input, max}` to the cook's status. Captured
    payload shape (Claude Code v2.1.119, redacted):

        {
          "session_id": "00000000-0000-0000-0000-000000000000",
          "transcript_path": "/tmp/example/<session-uuid>.jsonl",
          "cwd": "/repo",
          "permission_mode": "bypassPermissions",
          "hook_event_name": "Stop",
          "stop_hook_active": false,
          "last_assistant_message": "<redacted>"
        }

    No `usage` field on the payload itself, hence the JSONL read."""

    def test_stop_writes_tokens_from_transcript(self, monkeypatch, tmp_path):
        # Real input-side numbers from the captured transcript
        # (sum = 1 + 652 + 173374 = 174027 input tokens).
        transcript_path = _stage_transcript(tmp_path, model="claude-opus-4-7",
            usage={
                "input_tokens": 1,
                "cache_creation_input_tokens": 652,
                "cache_read_input_tokens": 173374,
                "output_tokens": 378,
                "service_tier": "standard",
            },
        )

        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        _stdin_payload(
            monkeypatch,
            hook_event_name="Stop",
            last_assistant_message="hi",
            session_id="00000000-0000-0000-0000-000000000000",
            transcript_path=transcript_path,
        )

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook"))

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["tokens"] == {"input": 174027, "max": 1_000_000}
        assert data["session_id"] == "00000000-0000-0000-0000-000000000000"

    def test_stop_writes_null_max_for_unknown_model(self, monkeypatch, tmp_path):
        transcript_path = _stage_transcript(tmp_path, model="claude-future-9000",
            usage={"input_tokens": 100, "cache_read_input_tokens": 0,
                   "cache_creation_input_tokens": 0, "output_tokens": 50},
        )
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        _stdin_payload(monkeypatch,
            hook_event_name="Stop", last_assistant_message="hi",
            session_id="00000000-0000-0000-0000-000000000000",
            transcript_path=transcript_path)

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook"))

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["tokens"] == {"input": 100, "max": None}

    def test_stop_with_missing_transcript_does_not_set_tokens(self, monkeypatch, tmp_path):
        """If the transcript file is missing (or transcript_path is empty),
        the Stop write proceeds without tokens — never crashes."""
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        _stdin_payload(monkeypatch,
            hook_event_name="Stop", last_assistant_message="hi",
            session_id="00000000-0000-0000-0000-000000000000",
            transcript_path=str(tmp_path / "nope.jsonl"))

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook"))

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert "tokens" not in data
        assert data["status"] == "idle"
        assert data["summary"] == "hi"


def _stage_codex_rollout(home, thread_id, *, info, day=None):
    """Stage a rollout JSONL under <home>/.codex/sessions/YYYY/MM/DD/.
    Mirrors the structurally-faithful captured shape from a real Codex
    CLI rollout (2026-04-29): event_msg lines wrap a `token_count` payload
    whose `info` field carries `total_token_usage` and
    `model_context_window`. Returns the path created."""
    day = day or datetime.now(timezone.utc).date()
    day_dir = home / ".codex" / "sessions" / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    day_dir.mkdir(parents=True, exist_ok=True)
    f = day_dir / f"rollout-2026-04-29T09-49-33-{thread_id}.jsonl"
    lines = [
        # Earlier event with info=null (real shape — rate-limit-only events
        # emit token_count with no usage). Must be skipped, not crashed on.
        {"timestamp": "2026-04-29T16:49:39.675Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": None,
                     "rate_limits": {"limit_id": "codex"}}},
        # The line we actually want — info populated.
        {"timestamp": "2026-04-29T16:53:19.732Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": info,
                     "rate_limits": {"limit_id": "codex"}}},
    ]
    f.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return f


class TestCodexTokenCapture:
    """Codex notify branch reads usage from rollout JSONL.

    Captured 2026-04-29 from a real prolite-plan rollout
    (~/.codex/sessions/2026/04/29/rollout-...-019dda25-574e-...jsonl):

        info.last_token_usage = {
          input_tokens: 67490, cached_input_tokens: 65408,
          output_tokens: 122, reasoning_output_tokens: 45,
          total_tokens: 67612,
        }
        info.total_token_usage = {
          input_tokens: 457361, cached_input_tokens: 401664,
          output_tokens: 5508, reasoning_output_tokens: 2356,
          total_tokens: 462869,   # cumulative running total
        }
        info.model_context_window = 258400

    For rotation decisions we need the CURRENT per-turn context size
    (last_token_usage.input_tokens = 67490), not the cumulative running
    total (total_token_usage.input_tokens = 457361 — meaningless against
    the 258k window since cumulative grows past it on long sessions).
    """

    def test_codex_notify_writes_tokens_from_rollout(self, monkeypatch, tmp_path):
        thread_id = "00000000-0000-0000-0000-000000000000"
        _stage_codex_rollout(tmp_path, thread_id, info={
            "total_token_usage": {
                "input_tokens": 457361,
                "cached_input_tokens": 401664,
                "output_tokens": 5508,
                "reasoning_output_tokens": 2356,
                "total_tokens": 462869,
            },
            "last_token_usage": {
                "input_tokens": 67490, "cached_input_tokens": 65408,
                "output_tokens": 122, "reasoning_output_tokens": 45,
                "total_tokens": 67612,
            },
            "model_context_window": 258400,
        })

        monkeypatch.setattr("claude_kitchen.cli.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("AGENT_NAME", "cx-eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path / "state"))
        payload = _codex_notify_payload(**{
            "thread-id": thread_id,
            "last-assistant-message": "<redacted>",
        })

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=payload))

        data = json.loads((tmp_path / "state" / "cooks" / "cx-eng.json").read_text())
        assert data["tokens"] == {"input": 67490, "max": 258400}
        assert data["session_id"] == thread_id
        assert data["status"] == "idle"

    def test_codex_notify_ignores_side_thread_without_rollout(self, monkeypatch, tmp_path):
        """Codex's title-generation side thread fires notify too, with its own
        thread-id and no rollout. It must not overwrite the cook's session_id
        or summary, nor reach the sous. Payload shapes captured live from
        codex-cli 0.162.1."""
        real = "01a1421f-77b9-78d2-aa82-2d92d0de0c16"
        _stage_codex_rollout(tmp_path, real, info=None)
        monkeypatch.setattr("claude_kitchen.cli.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.setenv("AGENT_NAME", "cx-eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path / "state"))

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=_codex_notify_payload(**{
                "thread-id": real, "client": "codex-tui",
                "last-assistant-message": "NOTIFY-PROBE-1",
            })))
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=_codex_notify_payload(**{
                "thread-id": "01a1421f-8113-7cb3-ada5-1605a20a0849", "client": "codex-tui",
                "last-assistant-message": '{"title":"Say NOTIFY-PROBE-1"}',
            })))

        data = json.loads((tmp_path / "state" / "cooks" / "cx-eng.json").read_text())
        assert data["session_id"] == real
        assert data["summary"] == "NOTIFY-PROBE-1"
        mock_send.assert_called_once()

    def test_codex_notify_accepts_pinned_thread_without_rollout(self, monkeypatch, tmp_path):
        """Once pinned, the cook's own thread keeps reporting even when its
        rollout has aged out of the lookup window — otherwise a long-lived
        cook goes mute."""
        real = "01a1421f-77b9-78d2-aa82-2d92d0de0c16"
        monkeypatch.setattr("claude_kitchen.cli.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.setenv("AGENT_NAME", "cx-eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path / "state"))
        write_status(tmp_path / "state", "cx-eng", {"status": "working", "backend": "codex",
                                                    "session_id": real})

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=_codex_notify_payload(**{
                "thread-id": real, "last-assistant-message": "nine days in",
            })))

        data = json.loads((tmp_path / "state" / "cooks" / "cx-eng.json").read_text())
        assert data["status"] == "idle"
        assert data["summary"] == "nine days in"
        mock_send.assert_called_once()

    def test_codex_rollout_found_under_codex_home(self, monkeypatch, tmp_path):
        """A clean-room cook exports its own CODEX_HOME; its rollout lives there."""
        home = tmp_path / "codex-home" / "eval1"
        f = _stage_codex_rollout(tmp_path, "33333333-3333-3333-3333-333333333333", info=None)
        target = home / "sessions" / f.relative_to(tmp_path / ".codex" / "sessions")
        target.parent.mkdir(parents=True)
        f.rename(target)
        monkeypatch.setenv("CODEX_HOME", str(home))
        assert _codex_rollout("33333333-3333-3333-3333-333333333333") == target

    def test_codex_notify_skips_info_null_lines(self, monkeypatch, tmp_path):
        """Only the rate-limit-only `info: null` line exists → no usable
        token_count, so tokens are omitted."""
        thread_id = "22222222-2222-2222-2222-222222222222"
        # Stage with info=None for both lines.
        _stage_codex_rollout(tmp_path, thread_id, info=None)

        monkeypatch.setattr("claude_kitchen.cli.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("AGENT_NAME", "cx-eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path / "state"))
        payload = _codex_notify_payload(**{
            "thread-id": thread_id,
            "last-assistant-message": "info-null only",
        })

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=payload))

        data = json.loads((tmp_path / "state" / "cooks" / "cx-eng.json").read_text())
        assert "tokens" not in data


class TestChannelCtxAttribute:
    """Both Stop (Claude) and notify (Codex) branches push `ctx` into the
    channel send_to_socket payload when tokens are present, and omit the
    key entirely when tokens are absent."""

    def test_claude_stop_includes_ctx_when_tokens_present(self, monkeypatch, tmp_path):
        # Stage a transcript with real-shape usage so Stop computes tokens.
        transcript_path = _stage_transcript(tmp_path, model="claude-opus-4-7",
            usage={
                "input_tokens": 1, "cache_creation_input_tokens": 652,
                "cache_read_input_tokens": 173374, "output_tokens": 378,
            })
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        _stdin_payload(monkeypatch,
            hook_event_name="Stop", last_assistant_message="ok",
            session_id="00000000-0000-0000-0000-000000000000",
            transcript_path=transcript_path)

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        push = mock_send.call_args[0][1]
        # 1 + 652 + 173374 = 174027 → floor(174027 * 100 / 1_000_000) = 17.
        assert push["ctx"] == "17% (174k/1000k)"
        assert push["cook"] == "eng"

    def test_claude_stop_omits_ctx_when_transcript_missing_and_no_prior(self, monkeypatch, tmp_path):
        """No prior tokens, transcript can't be read → ctx omitted entirely."""
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        _stdin_payload(monkeypatch,
            hook_event_name="Stop", last_assistant_message="ok",
            session_id="00000000-0000-0000-0000-000000000000",
            transcript_path=str(tmp_path / "vanished.jsonl"))

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        push = mock_send.call_args[0][1]
        assert "ctx" not in push, f"ctx must be absent, not null/empty; got: {push!r}"

    def test_codex_notify_includes_ctx_when_rollout_present(self, monkeypatch, tmp_path):
        thread_id = "00000000-0000-0000-0000-000000000000"
        _stage_codex_rollout(tmp_path, thread_id, info={
            "total_token_usage": {"input_tokens": 457361, "cached_input_tokens": 401664,
                                  "output_tokens": 5508, "reasoning_output_tokens": 2356,
                                  "total_tokens": 462869},
            "last_token_usage":  {"input_tokens": 67490, "cached_input_tokens": 65408,
                                  "output_tokens": 122, "reasoning_output_tokens": 45,
                                  "total_tokens": 67612},
            "model_context_window": 258400,
        })
        monkeypatch.setattr("claude_kitchen.cli.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("AGENT_NAME", "cx-eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path / "state"))
        payload = _codex_notify_payload(**{"thread-id": thread_id,
                                           "last-assistant-message": "<redacted>"})

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=payload))

        push = mock_send.call_args[0][1]
        # 67490 → floor(67490 * 100 / 258400) = 26.
        assert push["ctx"] == "26% (67k/258k)"

    def test_codex_notify_omits_ctx_when_tokens_missing(self, monkeypatch, tmp_path):
        _stage_codex_rollout(tmp_path, "11111111-1111-1111-1111-111111111111", info=None)
        monkeypatch.setattr("claude_kitchen.cli.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.setenv("AGENT_NAME", "cx-eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path / "state"))
        payload = _codex_notify_payload(**{
            "thread-id": "11111111-1111-1111-1111-111111111111",
            "last-assistant-message": "no rollout",
        })

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=payload))

        push = mock_send.call_args[0][1]
        assert "ctx" not in push


class TestStatusPreservationAcrossNonStopWriters:
    """Per spec §Status preservation rules, `tokens` and `backend` survive
    all non-completion status transitions."""

    def test_user_prompt_submit_preserves_tokens_and_backend(self, monkeypatch, tmp_path):
        # Seed a Stop-equivalent state with both durable fields.
        write_status(tmp_path, "eng", {
            "status": "idle", "agent": "eng", "backend": "claude",
            "tokens": {"input": 174027, "max": 1_000_000},
            "summary": "earlier summary",
        })

        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "hi"})
        monkeypatch.setattr("sys.stdin", MagicMock(read=MagicMock(return_value=payload)))

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook"))

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["status"] == "working"
        assert data["backend"] == "claude"
        assert data["tokens"] == {"input": 174027, "max": 1_000_000}

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_cmd_ticket_preserves_tokens_and_backend(
        self, mock_rk, mock_state, mock_send, tmp_path,
    ):
        """cmd_ticket flips a Codex cook to working but must not silently
        clobber durable fields. This is the realistic "ticket fired between
        turns" case for a cook with prior usage."""
        mock_state.return_value = tmp_path
        write_status(tmp_path, "eng", {
            "status": "idle", "agent": "eng", "backend": "codex",
            "tokens": {"input": 174027, "max": 1_000_000},
            "summary": "earlier",
        })

        from claude_kitchen.cli import cmd_ticket
        args = MagicMock()
        args.kitchen = "risotto"
        args.cook = "eng"
        args.message = "next task"
        args.chat_message = None
        args.priority = None
        cmd_ticket(args)

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["status"] == "working"
        assert data["backend"] == "codex"
        assert data["tokens"] == {"input": 174027, "max": 1_000_000}

    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_a_claude_turn_that_finishes_during_the_send_stays_idle(self, mock_rk, mock_state, tmp_path):
        # Its hooks write working then idle while send_keys is still returning.
        mock_state.return_value = tmp_path
        write_status(tmp_path, "eng", {"status": "idle", "agent": "eng", "backend": "claude"})

        def turn_runs(*_a, **_k):
            update_status(tmp_path, "eng", status="working")
            update_status(tmp_path, "eng", status="idle", summary="done")

        from claude_kitchen.cli import cmd_ticket
        with patch("claude_kitchen.cli.send_keys", side_effect=turn_runs):
            cmd_ticket(argparse.Namespace(kitchen="risotto", cook="eng", message="quick task", chat_message=None, priority=None, dangerously_skip_priority=True))
        assert json.loads((tmp_path / "cooks" / "eng.json").read_text())["status"] == "idle"

    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_chat_message_rides_back_on_the_next_report_only(self, mock_rk, mock_state, monkeypatch, tmp_path):
        mock_state.return_value = tmp_path
        write_status(tmp_path, "eng", {"status": "idle", "agent": "eng", "backend": "claude"})
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        pushes = []

        def report(*_a, **_k):
            _stdin_payload(monkeypatch, hook_event_name="Stop", last_assistant_message="done",
                session_id="00000000-0000-0000-0000-000000000000", transcript_path=str(tmp_path / "none.jsonl"))
            with patch("claude_kitchen.channel.send_to_socket", lambda _sock, push: pushes.append(push)):
                cmd_hook(argparse.Namespace(command="hook"))

        from claude_kitchen.cli import cmd_ticket
        # The cook's turn finishes while the ticket is still being sent.
        with patch("claude_kitchen.cli.send_keys", side_effect=report):
            cmd_ticket(argparse.Namespace(kitchen="risotto", cook="eng", message="fix it", chat_message="swarm !r:s $ev", priority=None, dangerously_skip_priority=True))
        report()
        assert [p.get("chat_message") for p in pushes] == ["swarm !r:s $ev", None]

    @pytest.mark.parametrize("source", ["swarm", "plow-chat"])
    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_a_ticketed_chat_message_gets_hot_springs_then_a_check_before_the_report(
            self, mock_rk, mock_state, mock_keys, monkeypatch, tmp_path, source):
        """The owner sees a cook take their message on (♨️) and finish it (✅), both landing before the
        sous's answer, so the answer stays the chat's latest preview."""
        mock_state.return_value = tmp_path
        write_status(tmp_path, "eng", {"status": "idle", "agent": "eng", "backend": "claude"})
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        marks = []
        if source == "swarm":
            monkeypatch.setattr("claude_kitchen.plow_chat.swarm_account", lambda _k: {"homeserver": "https://hq", "access_token": "t"})

            def put(_account, path, body):
                marks.append(("redact", path.split("/")[4]) if "/redact/" in path else ("react", body["m.relates_to"]["key"]))
                return {"event_id": f"$r{len(marks)}"}
            monkeypatch.setattr("claude_kitchen.plow_chat.matrix_put", put)
            chat_message = "swarm !room:hq $asked"
        else:
            def plow(request):
                body = json.loads(request.content)
                marks.append((body["operation"], body["custom_emoji"]))
                return httpx.Response(200, json={})
            monkeypatch.setattr("claude_kitchen.plow_chat.plow_client", lambda _k: httpx.Client(base_url="https://plow", transport=httpx.MockTransport(plow)))
            chat_message = "plow-chat cht_solo msg_1"

        from claude_kitchen.cli import cmd_ticket
        cmd_ticket(argparse.Namespace(kitchen="risotto", cook="eng", message="fix it", chat_message=chat_message, priority=None, dangerously_skip_priority=True))
        assert marks == ([("react", "♨️")] if source == "swarm" else [("add", "♨️")])

        pushes = []
        _stdin_payload(monkeypatch, hook_event_name="Stop", last_assistant_message="done",
                       session_id="00000000-0000-0000-0000-000000000000", transcript_path=str(tmp_path / "none.jsonl"))
        with patch("claude_kitchen.channel.send_to_socket", lambda _sock, push: pushes.append((list(marks), push))):
            cmd_hook(argparse.Namespace(command="hook"))
        done = [("react", "♨️"), ("react", "✅"), ("redact", "%24r1")] if source == "swarm" else [("add", "♨️"), ("add", "✅")]
        assert [(seen, push["chat_message"]) for seen, push in pushes] == [(done, chat_message)]

    def test_a_ticket_taken_while_the_report_marks_its_message_keeps_its_own(self, monkeypatch, tmp_path):
        """The cook is idle while its report's ✅ is on its way, so the sous can ticket it
        again then; that ticket's chat message must ride back on the next report."""
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        update_status(tmp_path, "eng", chat_message={"message": "swarm !r:s $first"})

        def ticketed_meanwhile(_kitchen, record, _emoji):
            update_status(tmp_path, "eng", chat_message={"message": "swarm !r:s $second"})
            return record
        monkeypatch.setattr("claude_kitchen.plow_chat.mark", ticketed_meanwhile)
        _stdin_payload(monkeypatch, hook_event_name="Stop", last_assistant_message="done")
        with patch("claude_kitchen.channel.send_to_socket", lambda _sock, _push: None):
            cmd_hook(argparse.Namespace(command="hook"))
        assert read_status(tmp_path, "eng")["chat_message"]["message"] == "swarm !r:s $second"

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_chat_message_refused_while_the_cook_is_mid_turn(self, mock_rk, mock_state, mock_keys, tmp_path):
        # Its current turn's report would otherwise carry the new ticket's message.
        mock_state.return_value = tmp_path
        write_status(tmp_path, "eng", {"status": "working", "agent": "eng", "backend": "claude"})
        from claude_kitchen.cli import cmd_ticket
        with pytest.raises(SystemExit, match="mid-turn"):
            cmd_ticket(argparse.Namespace(kitchen="risotto", cook="eng", message="fix it", chat_message="swarm !r:s $ev", priority=None, dangerously_skip_priority=True))
        assert not mock_keys.called
        assert "chat_message" not in read_status(tmp_path, "eng")

    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_wait_records_and_clears_by_message_id(self, mock_rk, mock_state, monkeypatch, tmp_path):
        from claude_kitchen.state import read_waits
        mock_state.return_value = tmp_path
        monkeypatch.setattr(sys, "argv", ["kitchen", "wait", "swarm", "!r:s", "$ev", "the OK", "--on", "jordan"])
        main()
        assert [(w["message"], w["on"], w["note"]) for w in read_waits(tmp_path)] == [("swarm !r:s $ev", "jordan", "the OK")]
        monkeypatch.setattr(sys, "argv", ["kitchen", "wait", "done", "$ev"])
        main()
        assert read_waits(tmp_path) == []
        with pytest.raises(SystemExit, match="Nothing is waiting"):
            main()

    def test_stop_with_failed_transcript_read_preserves_prior_tokens(self, monkeypatch, tmp_path):
        """Realistic 'turn N succeeded, turn N+1's transcript read fails'
        case. Prior `tokens` must survive — that's the whole reason the
        Stop write goes through update_status instead of write_status."""
        write_status(tmp_path, "eng", {
            "status": "idle", "agent": "eng", "backend": "claude",
            "tokens": {"input": 174027, "max": 1_000_000},
        })

        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        # Transcript path points at a file that doesn't exist — helper
        # returns None, Stop branch must not write tokens (preserving prior).
        _stdin_payload(monkeypatch,
            hook_event_name="Stop", last_assistant_message="next turn",
            session_id="00000000-0000-0000-0000-000000000000",
            transcript_path=str(tmp_path / "vanished.jsonl"))

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook"))

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["status"] == "idle"
        assert data["summary"] == "next turn"
        assert data["backend"] == "claude"
        assert data["tokens"] == {"input": 174027, "max": 1_000_000}, (
            "Prior tokens must survive a transcript-read miss"
        )

    @patch("claude_kitchen.cli.spawn_window", return_value=False)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_cmd_hire_failure_preserves_backend(
        self, mock_rp, mock_rk, mock_state, mock_spawn, tmp_path,
    ):
        """If spawn_window fails, the booting write's `backend` must
        survive into the failed write. Symmetric with the other
        non-completion preservation cases."""
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "eng"
        args.backend = "codex"
        args.project = None
        args.role = None
        args.effort = None
        args.clean_room = False
        args.with_skill = []; args.slot = None; args.swarm_account = None
        args.model = None
        with pytest.raises(SystemExit):
            cmd_hire(args)
        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["status"] == "failed"
        assert data["backend"] == "codex"

    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_cmd_hire_model_backend_mismatch_fails_loud(
        self, mock_rp, mock_rk, mock_state, tmp_path,
    ):
        """Each --model belongs to one backend: Claude tiers on codex/gemini
        and astra on claude/gemini must all fail loud."""
        for backend, model, want in (
            ("codex", "opus", "only supported for claude cooks"),
            ("gemini", "opus", "only supported for claude cooks"),
            ("claude", "astra", "only supported for codex cooks"),
            ("gemini", "astra", "only supported for codex cooks"),
        ):
            mock_state.return_value = tmp_path
            args = MagicMock()
            args.kitchen = "risotto"
            args.name = "eng"
            args.backend = backend
            args.project = None
            args.role = None
            args.effort = None
            args.clean_room = False
            args.with_skill = []; args.slot = None; args.swarm_account = None
            args.model = model
            with pytest.raises(SystemExit, match=want):
                cmd_hire(args)

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_cmd_hire_model_matching_backend_reaches_spawn(
        self, mock_rp, mock_rk, mock_state, mock_spawn, mock_wait, mock_send, tmp_path,
    ):
        """The passing side of the gate: a matched pairing — with or without
        --effort — reaches spawn_window with model and effort intact."""
        for backend, model, effort in (("claude", "opus", "high"),
                                       ("codex", "astra", "ultra"),
                                       ("codex", "astra", None)):
            mock_state.return_value = tmp_path
            args = MagicMock()
            args.kitchen = "risotto"
            args.name = f"{backend}-{effort}"
            args.backend = backend
            args.project = None
            args.role = None
            args.effort = effort
            args.clean_room = False
            args.with_skill = []; args.slot = None; args.swarm_account = None
            args.model = model
            cmd_hire(args)
            assert mock_spawn.call_args.kwargs["model"] == model
            assert mock_spawn.call_args.kwargs["effort"] == effort

    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_cmd_hire_slot_puts_the_cook_in_the_slot_tc_claims(
        self, mock_rk, mock_state, mock_spawn, mock_wait, tmp_path, monkeypatch,
    ):
        """--slot hands the cook the dir `tc claim` printed, branched for this
        cook and already trusted by Claude; a claim that finds no slot stops the
        hire before anything boots."""
        mock_state.return_value = tmp_path
        (tmp_path / ".claude.json").write_text(json.dumps({"numStartups": 3, "projects": {"/other": {"x": 1}}}))
        args = self._fake_tc(tmp_path, monkeypatch, "claude")
        cmd_hire(args)
        assert mock_spawn.call_args.kwargs["cwd"] == "/home/u/Hacking/plow3"
        # The record names the slot until clock-out, which is what keeps tc off it.
        assert json.loads((tmp_path / "cooks" / "eng.json").read_text())["cwd"] == "/home/u/Hacking/plow3"
        assert (tmp_path / "argv").read_text() == "claim plow risotto/eng\n"
        assert json.loads((tmp_path / ".claude.json").read_text()) == {
            "numStartups": 3,
            "projects": {"/other": {"x": 1}, "/home/u/Hacking/plow3": {"hasTrustDialogAccepted": True}},
        }

        mock_spawn.reset_mock()
        args.slot = "none"
        with pytest.raises(SystemExit, match="tc claim none failed for eng"):
            cmd_hire(args)
        mock_spawn.assert_not_called()

    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="widget-cos")
    def test_cmd_hire_swarm_account_gives_the_cook_its_own_bound_swarm_server(
        self, mock_rk, mock_state, mock_wait, tmp_path, monkeypatch,
    ):
        """The cook launches with an --mcp-config holding only swarm, bound to the
        account, then is asked to join that account's rooms; Codex is refused."""
        mock_state.return_value = tmp_path
        monkeypatch.setenv("HOME", str(tmp_path))
        credential = tmp_path / ".swarm/accounts/swarm.example.com/cedar/credential.json"
        credential.parent.mkdir(parents=True)
        credential.write_text(json.dumps({"user_id": "@cedar:c", "homeserver": "https://c", "access_token": "t"}))
        (tmp_path / "cooks").mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "swarm").write_text("#!/bin/sh\n")
        (bin_dir / "swarm").chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
        args = argparse.Namespace(kitchen=None, name="relay", backend="claude", project=str(tmp_path), slot=None,
                                  role=None, effort=None, clean_room=False, with_skill=[], model=None,
                                  swarm_account="@cedar:c")
        with patch("claude_kitchen.cli.spawn_window", return_value=True) as spawn, \
             patch("claude_kitchen.cli.swarm_rooms", return_value=["#general:c"]), \
             patch("claude_kitchen.cli.join_swarm") as join:
            cmd_hire(args)
        config = json.loads(spawn.call_args.kwargs["mcp_config"].read_text())
        assert config == {"mcpServers": {"swarm": {"command": str(bin_dir / "swarm"),
                                                   "args": ["mcp", "claude", "--account", "@cedar:c"]}}}
        # Outside the cooks/*.json namespace, which sweep and brigade read as cook records.
        assert spawn.call_args.kwargs["mcp_config"].name == "relay.mcp"
        kitchen, window, account, prompt = join.call_args.args
        assert (window, account) == ("relay", "@cedar:c") and "#general:c" in prompt

        args.name, args.backend = "relay2", "codex"
        with pytest.raises(SystemExit, match="--swarm-account is for Claude cooks"):
            cmd_hire(args)
        assert not (tmp_path / "cooks" / "relay2.json").exists()  # refused before any cook record

    @staticmethod
    def _fake_tc(tmp_path, monkeypatch, backend):
        """HOME at tmp_path and a kitchen `tc` that records its args and claims plow3
        (or fails for slot `none`); returns `--slot plow` hire args."""
        monkeypatch.setenv("HOME", str(tmp_path))
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        tc = bin_dir / "tc"
        tc.write_text(f'#!/bin/sh\necho "$@" > {tmp_path}/argv\n[ "$2" = none ] && exit 1\necho /home/u/Hacking/plow3\n')
        tc.chmod(0o755)
        monkeypatch.setattr("claude_kitchen.cli.TC", tc)
        return argparse.Namespace(kitchen="risotto", name="eng", backend=backend, project=None,
                                  slot="plow", role=None, effort=None, clean_room=False,
                                  with_skill=[], model=None, swarm_account=None)

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_cmd_hire_slot_trusts_a_codex_cooks_slot_in_codex_config(
        self, mock_rk, mock_state, mock_spawn, mock_wait, mock_send, tmp_path, monkeypatch,
    ):
        """Codex keeps its own trust: a codex --slot cook's dir is appended to
        ~/.codex/config.toml once, and a dir that already has an entry is left
        alone (a second table for it would break the whole file)."""
        mock_state.return_value = tmp_path
        args = self._fake_tc(tmp_path, monkeypatch, "codex")
        (tmp_path / ".codex").mkdir()
        config = tmp_path / ".codex" / "config.toml"
        config.write_text('model = "x"\n\n[projects."/home/u/Hacking/plow33"]\ntrust_level = "trusted"\n')
        cmd_hire(args)
        cmd_hire(args)
        assert tomllib.loads(config.read_text())["projects"] == {
            "/home/u/Hacking/plow33": {"trust_level": "trusted"}, "/home/u/Hacking/plow3": {"trust_level": "trusted"}}

        config.write_text('[projects."/home/u/Hacking/plow3"]\ntrust_level = "untrusted"\n')
        cmd_hire(args)
        assert tomllib.loads(config.read_text())["projects"] == {"/home/u/Hacking/plow3": {"trust_level": "untrusted"}}

    def test_concurrent_slot_hires_each_keep_their_trust_grant(self, tmp_path):
        """Kitchens hiring at once must not drop each other's trust grant: a lost
        one stops that cook at Claude's folder-trust dialog."""
        (tmp_path / ".claude.json").write_text(json.dumps({"projects": {f"/old{i}": {} for i in range(20000)}}))
        env = {**os.environ, "HOME": str(tmp_path)}
        hires = [subprocess.Popen([sys.executable, "-c", f"from claude_kitchen.cli import _trust_dir; _trust_dir('/slot{i}', 'claude')"], env=env)
                 for i in range(8)]
        assert [p.wait() for p in hires] == [0] * 8
        projects = json.loads((tmp_path / ".claude.json").read_text())["projects"]
        assert all(projects[f"/slot{i}"] == {"hasTrustDialogAccepted": True} for i in range(8))

class TestBrigadeAlignedOutput:
    """Per spec §Chunk 4: cmd_brigade output is one row per cook,
    column-aligned `<cook-name>  <status>  <ctx>`. Cook-name padded to
    the longest name in the listing; status padded to a fixed 7-char
    width (longest of working/booting/unknown/failed/idle) so ctx
    aligns vertically. No summary suffix."""

    @patch("claude_kitchen.cli.list_kitchens", return_value=["risotto"])
    @patch("claude_kitchen.cli.list_windows",
           return_value=["alpha", "longername", "x", "frsh"])
    @patch("claude_kitchen.cli.read_status")
    def test_aligned_columns_three_ctx_cases(self, mock_status, mock_win, mock_sess, capsys):
        # Cook order matches list_windows; mix of statuses + ctx cases.
        mock_status.side_effect = [
            {"status": "idle",   "tokens": {"input": 174027, "max": 1_000_000},
             "summary": "should not appear"},  # summary must be dropped
            {"status": "working","tokens": {"input": 12345,  "max": None}},  # unknown model
            {"status": "booting"},                                            # no tokens
            {"status": "failed", "tokens": {"input": 0, "max": 200_000}},
        ]
        cmd_brigade(kitchen=None)
        out = capsys.readouterr().out

        # Longest name is 'longername' (10 chars); status pad is 7.
        # Two-space separators per the spec.
        assert "  alpha       idle     17% (174k/1000k)" in out, out
        assert "  longername  working  12k (unknown)" in out, out
        assert "  x           booting  —" in out, out
        assert "  frsh        failed   0% (0k/200k)" in out, out
        # Summary is dropped from default brigade output.
        assert "should not appear" not in out
        assert '-- "' not in out


class TestCmdOpen:
    @patch("claude_kitchen.cli.namespaced", return_value="widget-risotto")
    @patch("claude_kitchen.cli.project_slug", return_value="widget")
    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.create_worktree", return_value=Path("/tmp/risotto"))
    @patch("claude_kitchen.cli.resolve_project")
    def test_writes_mcp_config_and_execs(self, mock_resolve, mock_wt, mock_state, mock_tmux, mock_has, mock_spawn, mock_slug, mock_ns, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resolve.return_value = Path("/tmp/myproject")
        mock_state.return_value = tmp_path
        mock_tmux.return_value = MagicMock(returncode=0)

        args = MagicMock()
        args.name = "risotto"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False

        cmd_open(args)

        # renamed MCP config written to state dir (NOT a discoverable
        # .mcp.json — cooks must never auto-discover it)
        mcp_config = tmp_path / "kitchen-mcp.json"
        assert mcp_config.exists()
        assert not (tmp_path / ".mcp.json").exists()
        config = json.loads(mcp_config.read_text())
        assert "kitchen" in config["mcpServers"]

        # spawn_sous called with the namespaced kitchen name, state dir,
        # prompt, and worktree path (worktree keeps the bare `requested` name)
        mock_spawn.assert_called_once()
        call_args = mock_spawn.call_args
        assert call_args[0][0] == "widget-risotto"
        assert call_args[0][1] == tmp_path
        assert call_args[0][3] == Path("/tmp/risotto")

    @patch("claude_kitchen.cli.namespaced", return_value="widget-risotto")
    @patch("claude_kitchen.cli.project_slug", return_value="widget")
    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.create_worktree", return_value=Path("/tmp/risotto"))
    @patch("claude_kitchen.cli.resolve_project")
    def test_open_self_heals_legacy_mcp_config(self, mock_resolve, mock_wt, mock_state, mock_tmux, mock_has, mock_spawn, mock_slug, mock_ns, tmp_path, monkeypatch):
        # A kitchen opened before the rename has a stale, cook-discoverable
        # base/.mcp.json. Opening (and resuming — the writer/unlink runs
        # unconditionally before the resume branch) must delete it.
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resolve.return_value = Path("/tmp/myproject")
        mock_state.return_value = tmp_path
        mock_tmux.return_value = MagicMock(returncode=0)
        legacy = tmp_path / ".mcp.json"
        legacy.write_text('{"mcpServers": {}}')
        # pre-existing kitchen.json => resuming=True path
        (tmp_path / "kitchen.json").write_text(
            json.dumps({"source": "/tmp/myproject", "slug": "widget"})
        )

        args = MagicMock()
        args.name = "risotto"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False

        cmd_open(args)

        assert not legacy.exists(), "legacy .mcp.json must be self-healed on open/resume"
        assert (tmp_path / "kitchen-mcp.json").exists()


class TestSweepCooks:
    def _populate(self, base, names):
        cooks = base / "cooks"
        cooks.mkdir(parents=True)
        for n in names:
            (cooks / f"{n}.json").write_text("{}")
        return cooks

    @patch("claude_kitchen.cli.list_windows")
    def test_deletes_orphans_keeps_live(self, mock_lw, tmp_path):
        cooks = self._populate(tmp_path, ["alpha", "beta", "gamma"])
        mock_lw.return_value = ["alpha"]
        _sweep_cooks(tmp_path, "ck-x")
        survivors = sorted(f.stem for f in cooks.glob("*.json"))
        assert survivors == ["alpha"]

    @patch("claude_kitchen.cli.list_windows")
    def test_noop_when_all_live(self, mock_lw, tmp_path):
        cooks = self._populate(tmp_path, ["alpha", "beta"])
        mock_lw.return_value = ["alpha", "beta", "unrelated"]
        _sweep_cooks(tmp_path, "ck-x")
        assert sorted(f.stem for f in cooks.glob("*.json")) == ["alpha", "beta"]

    @patch("claude_kitchen.cli.list_windows")
    def test_deletes_all_when_no_live_windows(self, mock_lw, tmp_path):
        cooks = self._populate(tmp_path, ["alpha", "beta"])
        mock_lw.return_value = []
        _sweep_cooks(tmp_path, "ck-x")
        assert list(cooks.glob("*.json")) == []

    @patch("claude_kitchen.cli.list_windows")
    def test_noop_when_cooks_dir_missing(self, mock_lw, tmp_path):
        mock_lw.return_value = ["alpha"]
        _sweep_cooks(tmp_path, "ck-x")  # must not raise


class TestCmdSweep:
    @patch("claude_kitchen.cli.list_windows", return_value=["alpha"])
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_deletes_ghost_and_prints_summary(
        self, mock_resolve, mock_state, mock_lw, tmp_path, capsys,
    ):
        mock_state.return_value = tmp_path
        cooks = tmp_path / "cooks"
        cooks.mkdir()
        (cooks / "alpha.json").write_text("{}")
        (cooks / "ghost.json").write_text("{}")

        args = MagicMock()
        args.kitchen = None
        cmd_sweep(args)

        assert not (cooks / "ghost.json").exists()
        assert (cooks / "alpha.json").exists()
        out = capsys.readouterr().out
        assert "Swept 1 stale cook(s): ghost" in out


class TestCmdOpenNoOriginRepo:
    def test_succeeds_and_writes_fallback_slug(self, tmp_path, monkeypatch):
        repo = tmp_path / "myrepo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        monkeypatch.setenv("HOME", str(tmp_path))

        args = MagicMock()
        args.name = None
        args.project = str(repo)
        args.worktree_path = None
        args.resume = False

        state = tmp_path / "state"
        (state / "cooks").mkdir(parents=True)
        (state / "cooks" / "ghost.json").write_text("{}")

        with patch("claude_kitchen.cli.resolve_project", return_value=repo), \
             patch("claude_kitchen.cli.state_dir", return_value=state), \
             patch("claude_kitchen.cli.spawn_sous"), \
             patch("claude_kitchen.cli.has_session", return_value=False), \
             patch("claude_kitchen.cli.list_windows", return_value=[]), \
             patch("claude_kitchen.cli.tmux", return_value=MagicMock(returncode=0)):
            cmd_open(args)

        kj = json.loads((state / "kitchen.json").read_text())
        assert kj["slug"], "slug should be derived via full-path fallback, not empty"
        assert "-" in kj["slug"] and kj["slug"].endswith("myrepo")
        # has_session is False here, so this open CREATED the session — and an
        # open that had to create the session must not sweep. Every cook record
        # looks orphaned by construction there, including records for cooks that
        # are alive on a socket this tmux can't see. `kitchen sweep` still
        # clears them on demand (see TestCmdSweep above).
        assert (state / "cooks" / "ghost.json").exists()


class TestCmdOpenPlainFolder:
    """A folder that is not a git repository (real git, no mocks for it)."""

    def _args(self, folder, name):
        args = MagicMock()
        args.name = name
        args.project = str(folder)
        args.worktree_path = None
        args.resume = False
        return args

    def _open(self, folder, name, state):
        with patch("claude_kitchen.cli.state_dir", return_value=state), \
             patch("claude_kitchen.cli.spawn_sous") as spawn, \
             patch("claude_kitchen.cli.has_session", return_value=False), \
             patch("claude_kitchen.cli.tmux", return_value=MagicMock(returncode=0)) as tmux:
            cmd_open(self._args(folder, name))
        return spawn, tmux

    @pytest.mark.parametrize("flag, sous_toml, want", [
        (None, None, None),
        (None, 'model = "sonnet"\n', "sonnet"),
        ("opus", 'model = "sonnet"\n', "opus"),
    ])
    def test_open_launches_the_sous_on_the_flag_else_the_committed_model(
            self, tmp_path, monkeypatch, flag, sous_toml, want):
        monkeypatch.setenv("HOME", str(tmp_path))
        folder = tmp_path / "notetaker"
        (folder / ".kitchen").mkdir(parents=True)
        if sous_toml:
            (folder / ".kitchen" / "sous.toml").write_text(sous_toml)
        args = self._args(folder, None)
        args.model = flag
        with patch("claude_kitchen.cli.state_dir", return_value=tmp_path / "state"), \
             patch("claude_kitchen.cli.spawn_sous") as spawn, \
             patch("claude_kitchen.cli.has_session", return_value=False), \
             patch("claude_kitchen.cli.tmux", return_value=MagicMock(returncode=0)):
            cmd_open(args)
        assert spawn.call_args.kwargs["model"] == want

    def test_reopen_takes_the_model_from_the_kitchens_stored_worktree(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        source, worktree, state = tmp_path / "source", tmp_path / "worktree", tmp_path / "state"
        source.mkdir()
        (worktree / ".kitchen").mkdir(parents=True)
        (worktree / ".kitchen" / "sous.toml").write_text('model = "sonnet"\n')
        state.mkdir()
        (state / "kitchen.json").write_text(json.dumps({"source": str(source), "worktree": str(worktree)}))
        args = self._args(source, None)
        args.model = None
        with patch("claude_kitchen.cli.state_dir", return_value=state), \
             patch("claude_kitchen.cli.spawn_sous") as spawn, \
             patch("claude_kitchen.cli.has_session", return_value=False), \
             patch("claude_kitchen.cli.project_slug", return_value="x-source"), \
             patch("claude_kitchen.cli.tmux", return_value=MagicMock(returncode=0)):
            cmd_open(args)
        assert spawn.call_args.kwargs["model"] == "sonnet"

    def test_open_refuses_a_committed_model_it_does_not_know(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        folder = tmp_path / "notetaker"
        (folder / ".kitchen").mkdir(parents=True)
        (folder / ".kitchen" / "sous.toml").write_text('model = "haiku"\n')
        args = self._args(folder, None)
        args.model = None
        with patch("claude_kitchen.cli.state_dir", return_value=tmp_path / "state"), \
             patch("claude_kitchen.cli.spawn_sous") as spawn, \
             patch("claude_kitchen.cli.has_session", return_value=False), \
             patch("claude_kitchen.cli.tmux", return_value=MagicMock(returncode=0)), \
             pytest.raises(SystemExit, match="model must be one of fable, sonnet, opus"):
            cmd_open(args)
        spawn.assert_not_called()
        assert not (tmp_path / "state").exists()

    def test_unnamed_open_launches_the_sous_keyed_by_the_folder_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        folder = tmp_path / "marketing"
        folder.mkdir()
        state = tmp_path / "state"

        spawn, _ = self._open(folder, None, state)

        kj = json.loads((state / "kitchen.json").read_text())
        assert kj["source"] == str(folder.resolve())
        assert kj["slug"].endswith("-marketing") and "worktree" not in kj
        spawn.assert_called_once()
        assert spawn.call_args[0][3] == folder.resolve()

    def test_named_open_fails_clearly_and_leaves_nothing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        folder = tmp_path / "marketing"
        folder.mkdir()
        state = tmp_path / "state"

        with pytest.raises(SystemExit, match="Not a git repo"):
            spawn, tmux = self._open(folder, "hero", state)

        assert not state.exists()
        assert not (tmp_path / "hero").exists()


class TestCmdOpenFailures:
    @patch("claude_kitchen.cli.namespaced", return_value="widget-risotto")
    @patch("claude_kitchen.cli.project_slug", return_value="widget")
    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.create_worktree", return_value=Path("/tmp/risotto"))
    @patch("claude_kitchen.cli.resolve_project")
    def test_exits_when_sous_chef_md_missing(self, mock_resolve, mock_wt, mock_state, mock_tmux, mock_has, mock_slug, mock_ns, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resolve.return_value = Path("/tmp/myproject")
        mock_state.return_value = tmp_path
        mock_tmux.return_value = MagicMock(returncode=0)
        args = MagicMock()
        args.name = "risotto"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False
        with patch("claude_kitchen.cli._PKG_DIR", tmp_path):
            with pytest.raises(SystemExit, match="sous-chef.md not found"):
                cmd_open(args)


class TestCmdOpenWikiAndNotes:
    @patch("claude_kitchen.cli.namespaced", return_value="widget-risotto")
    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.create_worktree", return_value=Path("/tmp/risotto"))
    @patch("claude_kitchen.cli.resolve_project")
    @patch("claude_kitchen.cli.project_slug", return_value="widget")
    def test_creates_wiki_and_notes(
        self, mock_slug, mock_resolve, mock_wt, mock_state,
        mock_tmux, mock_has, mock_spawn, mock_ns, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resolve.return_value = Path("/tmp/myproject")
        mock_state.return_value = tmp_path / ".claude-kitchen" / "widget-risotto"
        mock_tmux.return_value = MagicMock(returncode=0)

        args = MagicMock()
        args.name = "risotto"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False

        cmd_open(args)

        # Wiki keys off the project slug; notes off the (namespaced) kitchen name.
        wiki = tmp_path / ".claude-kitchen" / "projects" / "widget" / "wiki"
        notes = tmp_path / ".claude-kitchen" / "widget-risotto" / "notes"
        assert wiki.is_dir()
        assert not (wiki / "mistakes.md").exists(), "legacy memory files are retired: learnings go to runbooks"
        assert not (wiki / "preferences.md").exists()
        assert (notes / "handoff.md").exists()
        assert (notes / "log.md").exists()

        kj = json.loads((mock_state.return_value / "kitchen.json").read_text())
        assert kj["slug"] == "widget"

    @patch("claude_kitchen.cli.namespaced", return_value="widget-risotto")
    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_project")
    @patch("claude_kitchen.cli.project_slug", return_value="widget")
    def test_resume_reads_stored_slug(
        self, mock_slug, mock_resolve, mock_state,
        mock_tmux, mock_has, mock_spawn, mock_ns, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resolve.return_value = Path("/tmp/myproject")
        base = tmp_path / ".claude-kitchen" / "widget-risotto"
        base.mkdir(parents=True)
        (base / "kitchen.json").write_text(json.dumps({
            "source": "/tmp/myproject", "slug": "widget",
        }) + "\n")
        mock_state.return_value = base
        mock_tmux.return_value = MagicMock(returncode=0)

        args = MagicMock()
        args.name = "risotto"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False

        cmd_open(args)
        # The resuming branch rewrites kitchen.json with the re-derived slug.
        # Verify the rewrite actually happened and preserved the source field.
        kj = json.loads((base / "kitchen.json").read_text())
        assert (kj["source"], kj["slug"]) == ("/tmp/myproject", "widget")

    @patch("claude_kitchen.cli.namespaced", return_value="renamed-risotto")
    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_project")
    @patch("claude_kitchen.cli.project_slug", return_value="renamed")
    def test_drift_fails_loudly(
        self, mock_slug, mock_resolve, mock_state,
        mock_tmux, mock_has, mock_spawn, mock_ns, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resolve.return_value = Path("/tmp/myproject")
        base = tmp_path / ".claude-kitchen" / "renamed-risotto"
        base.mkdir(parents=True)
        (base / "kitchen.json").write_text(json.dumps({
            "source": "/tmp/myproject", "slug": "widget",
        }) + "\n")
        mock_state.return_value = base
        mock_tmux.return_value = MagicMock(returncode=0)

        args = MagicMock()
        args.name = "risotto"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False

        with pytest.raises(SystemExit, match=r"Run `kitchen close renamed-risotto` and reopen\."):
            cmd_open(args)


class TestCmdOpenSoftCutover:
    """A pre-namespacing bare-name kitchen owned by this project is
    re-attached (with a deprecation suggestion) instead of forked."""

    @patch("claude_kitchen.cli.namespaced", return_value="proj-foo")
    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.list_windows", return_value=[])
    @patch("claude_kitchen.cli.tmux", return_value=MagicMock(returncode=0))
    @patch("claude_kitchen.cli.project_slug", return_value="proj")
    @patch("claude_kitchen.cli.resolve_project")
    def test_attaches_legacy_bare_kitchen(
        self, mock_resolve, mock_slug, mock_tmux, mock_lw, mock_spawn, mock_ns,
        tmp_path, monkeypatch, capsys,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        proj = tmp_path / "proj"
        proj.mkdir()
        mock_resolve.return_value = proj

        ns_base = tmp_path / ".claude-kitchen" / "proj-foo"      # namespaced: absent
        bare_base = tmp_path / ".claude-kitchen" / "foo"          # legacy: present
        bare_base.mkdir(parents=True)
        (bare_base / "kitchen.json").write_text(json.dumps({
            "source": str(proj), "slug": "old-long-slug",
        }) + "\n")

        def state_dir_for(name):
            return {"proj-foo": ns_base, "foo": bare_base}[name]

        # Only the legacy bare session is live; the namespaced one is not.
        def has_session_for(kitchen, **kw):
            return kitchen == "foo"

        args = MagicMock()
        args.name = "foo"
        args.project = str(proj)
        args.worktree_path = None
        args.resume = False

        with patch("claude_kitchen.cli.state_dir", side_effect=state_dir_for), \
             patch("claude_kitchen.cli.has_session", side_effect=has_session_for):
            cmd_open(args)

        out = capsys.readouterr().out
        assert "predates namespacing" in out
        assert "proj-foo" in out
        # Attached to the legacy bare kitchen, not the namespaced fork.
        assert mock_spawn.call_args[0][0] == "foo"
        # No namespaced kitchen forked alongside it.
        assert not (ns_base / "kitchen.json").exists()
        # Legacy slug refreshed to the new short form (drift guard bypassed).
        kj = json.loads((bare_base / "kitchen.json").read_text())
        assert kj["slug"] == "proj"


class TestResolveKitchenProbe:
    @patch("claude_kitchen.cli.namespaced", return_value="proj-foo")
    @patch("claude_kitchen.cli._cwd_project", return_value=Path("/proj"))
    def test_probes_namespaced_when_bare_absent(self, mock_cwd, mock_ns):
        # bare ck-foo missing, namespaced ck-proj-foo present → resolve to it.
        # has_session now takes a bare kitchen name — the socket is derived inside.
        def has_session_for(kitchen, **kw):
            return kitchen == "proj-foo"
        with patch("claude_kitchen.cli.has_session", side_effect=has_session_for):
            assert resolve_kitchen("foo") == "proj-foo"

    @patch("claude_kitchen.cli.namespaced", return_value="proj-foo")
    @patch("claude_kitchen.cli._cwd_project", return_value=Path("/proj"))
    def test_prefers_live_bare_session(self, mock_cwd, mock_ns):
        # A legacy bare session that's live keeps being targeted directly.
        with patch("claude_kitchen.cli.has_session", return_value=True):
            assert resolve_kitchen("foo") == "foo"

    @patch("claude_kitchen.cli.namespaced", return_value="proj-foo")
    @patch("claude_kitchen.cli._cwd_project", return_value=Path("/proj"))
    def test_returns_bare_when_neither_exists(self, mock_cwd, mock_ns):
        with patch("claude_kitchen.cli.has_session", return_value=False):
            assert resolve_kitchen("foo") == "foo"

    @patch("claude_kitchen.cli._cwd_project", return_value=None)
    def test_no_probe_outside_project_root(self, mock_cwd):
        with patch("claude_kitchen.cli.has_session", return_value=False):
            assert resolve_kitchen("foo") == "foo"


class TestCmdHireFailures:
    @pytest.mark.parametrize("outcome, error", [
        (False, "didn't show prompt"),
        (PromptBlocked("folder-trust", "/work/x"), "one-time folder-trust prompt.*cd /work/x && claude"),
    ], ids=["timeout", "one-time-prompt"])
    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.wait_for_prompt")
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_exits_on_a_cook_that_never_reaches_its_prompt(self, mock_rk, mock_state, mock_spawn, mock_wait, mock_send, outcome, error, tmp_path):
        mock_state.return_value = tmp_path
        if isinstance(outcome, Exception):
            mock_wait.side_effect = outcome
        else:
            mock_wait.return_value = outcome
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "eng"
        args.backend = "claude"
        args.project = None
        args.role = None
        args.effort = None
        args.clean_room = False
        args.with_skill = []; args.slot = None; args.swarm_account = None
        args.model = None
        with patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp")):
            with pytest.raises(SystemExit, match=error):
                cmd_hire(args)
        status = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert status["status"] == "failed"


class TestCmdHireRole:
    @patch("claude_kitchen.cli.spawn_window")
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_unknown_role_fails_with_valid_list(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, tmp_path,
    ):
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "eng"
        args.backend = "claude"
        args.project = None
        args.role = "ghost"
        args.effort = None
        args.clean_room = False
        args.with_skill = []; args.slot = None; args.swarm_account = None
        args.model = None
        with pytest.raises(SystemExit, match="Unknown role.*ghost"):
            cmd_hire(args)

    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_role_resolves_and_passes_path_to_spawn(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "rev1"
        args.backend = "claude"
        args.project = None
        args.role = "reviewer"
        args.effort = None
        args.clean_room = False
        args.with_skill = []; args.slot = None; args.swarm_account = None
        args.model = None
        cmd_hire(args)
        # The cook loads its role followed by the contract every cook shares.
        prompt = mock_spawn.call_args.kwargs["role_path"].read_text()
        assert prompt.startswith("# reviewer")
        assert "You are a cook, not a sous" in prompt and "End every report with `STATUS: DONE`" in prompt

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_codex_role_delivered_via_send_keys(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, mock_send, tmp_path,
    ):
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "rev-codex"
        args.backend = "codex"
        args.project = None
        args.role = "reviewer"
        args.effort = None
        args.clean_room = False
        args.with_skill = []; args.slot = None; args.swarm_account = None
        args.model = None
        cmd_hire(args)
        # Codex doesn't get a --append-system-prompt-file flag
        assert mock_spawn.call_args.kwargs["role_path"] is None
        # Thread named first, then role content delivered via send_keys
        assert [c.args[2] for c in mock_send.call_args_list][0] == "/rename [risotto/rev-codex]"
        assert mock_send.call_count == 2
        args_call = mock_send.call_args.args
        assert args_call[0] == "risotto"
        assert args_call[1] == "rev-codex"
        # role content + ack footer both arrive in one send
        assert "reviewer" in args_call[2].lower()
        # anchor on the H1, not prose: role bodies get rewritten, headers don't
        assert args_call[2].startswith("# reviewer")
        assert "Ready, chef." in args_call[2]

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_codex_default_role_sent_via_send_keys(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, mock_send, tmp_path,
    ):
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "cook1"
        args.backend = "codex"
        args.project = None
        args.role = None
        args.effort = None
        args.clean_room = False
        args.with_skill = []; args.slot = None; args.swarm_account = None
        args.model = None
        cmd_hire(args)
        assert mock_send.call_count == 2
        assert "_default — generic cook" in mock_send.call_args.args[2]

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_claude_does_not_send_keys(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, mock_send, tmp_path,
    ):
        # Claude gets role via --append-system-prompt-file; no send_keys needed.
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "eng1"
        args.backend = "claude"
        args.project = None
        args.role = "eng"
        args.effort = None
        args.clean_room = False
        args.with_skill = []; args.slot = None; args.swarm_account = None
        args.model = None
        cmd_hire(args)
        mock_send.assert_not_called()


class TestCmdHireCleanRoom:
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_clean_room_forwarded_and_no_role(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, tmp_path,
    ):
        """--clean-room must reach spawn_window as clean_room=True, and the cook
        boots bare — no role file resolved or passed."""
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "eval1"
        args.backend = "claude"
        args.project = None
        args.role = None
        args.effort = None
        args.clean_room = True
        args.with_skill = []; args.slot = None; args.swarm_account = None
        args.model = None
        cmd_hire(args)
        kwargs = mock_spawn.call_args.kwargs
        assert kwargs["clean_room"] is True
        assert kwargs["role_path"] is None

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/eval/dir"))
    def test_codex_clean_room_seeds_home_and_skips_role(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, mock_send, tmp_path, monkeypatch,
    ):
        """Clean-room codex: a fresh per-cook CODEX_HOME is seeded with ONLY
        auth.json + a cwd trust grant, forwarded to spawn_window, and the role
        send is skipped (cook boots bare)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "auth.json").write_text('{"OPENAI_API_KEY": "x"}')
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "eval1"
        args.backend = "codex"
        args.project = None
        args.role = None
        args.effort = None
        args.clean_room = True
        args.with_skill = []; args.slot = None; args.swarm_account = None
        args.model = None
        cmd_hire(args)
        home = tmp_path / "codex-home" / "eval1"
        assert (home / "auth.json").read_text() == '{"OPENAI_API_KEY": "x"}'
        cfg = (home / "config.toml").read_text()
        assert 'trust_level = "trusted"' in cfg
        assert '/eval/dir' in cfg  # trust granted to the cook's cwd
        assert mock_spawn.call_args.kwargs["codex_home"] == str(home)
        mock_send.assert_not_called()  # clean-room codex boots bare, no role

    def test_seed_codex_home_escapes_tricky_path(self, tmp_path, monkeypatch):
        """A cwd containing a double-quote, backslash, and space must yield
        VALID TOML whose projects key round-trips to the exact cwd — otherwise
        codex can't parse the trust grant and hits the blocking prompt."""
        from claude_kitchen.cli import _seed_codex_home
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "auth.json").write_text('{"OPENAI_API_KEY": "x"}')
        base = tmp_path / "base"; base.mkdir()
        tricky = '/tmp/we"ird\\path dir'
        home = _seed_codex_home(base, "eval1", tricky)
        parsed = tomllib.loads((home / "config.toml").read_text())  # raises if invalid TOML
        assert list(parsed["projects"].keys()) == [tricky]
        assert parsed["projects"][tricky]["trust_level"] == "trusted"

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.spawn_window", return_value=False)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/eval/dir"))
    def test_clean_room_codex_cleans_home_on_spawn_failure(
        self, mock_rp, mock_rk, mock_state, mock_spawn, mock_wait, mock_send, tmp_path, monkeypatch,
    ):
        """A failed launch must NOT leave the copied credential behind."""
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "auth.json").write_text('{"OPENAI_API_KEY": "x"}')
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"; args.name = "eval1"; args.backend = "codex"
        args.project = None; args.role = None; args.effort = None; args.clean_room = True; args.with_skill = []; args.slot = None; args.swarm_account = None
        args.model = None  # else the non-Claude --model guard exits before the codex-home path
        with pytest.raises(SystemExit):
            cmd_hire(args)
        mock_spawn.assert_called_once()  # proves we reached the spawn-failure path, not the guard
        assert not (tmp_path / "codex-home" / "eval1").exists(), "seeded CODEX_HOME (with auth.json) leaked on spawn failure"

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=False)
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/eval/dir"))
    def test_clean_room_codex_cleans_home_on_prompt_timeout(
        self, mock_rp, mock_rk, mock_state, mock_spawn, mock_wait, mock_send, tmp_path, monkeypatch,
    ):
        """A prompt-wait timeout must also tear down the seeded CODEX_HOME."""
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "auth.json").write_text('{"OPENAI_API_KEY": "x"}')
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"; args.name = "eval1"; args.backend = "codex"
        args.project = None; args.role = None; args.effort = None; args.clean_room = True; args.with_skill = []; args.slot = None; args.swarm_account = None
        args.model = None  # else the non-Claude --model guard exits before the prompt-wait path
        with pytest.raises(SystemExit):
            cmd_hire(args)
        mock_wait.assert_called_once()  # proves we reached the prompt-timeout path, not the guard
        assert not (tmp_path / "codex-home" / "eval1").exists(), "seeded CODEX_HOME leaked on prompt timeout"

    @patch("claude_kitchen.cli.spawn_window")
    @patch("claude_kitchen.cli.shutil.which", return_value="/usr/bin/agy")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_clean_room_gemini_fails_loud(
        self, mock_rp, mock_rk, mock_state, mock_which, mock_spawn, tmp_path,
    ):
        """Clean-room supports claude + codex; gemini still fails loud before
        spawning anything (guard runs before the agy-on-PATH check)."""
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "x"
        args.backend = "gemini"
        args.project = None
        args.role = None
        args.effort = None
        args.clean_room = True
        args.with_skill = []; args.slot = None; args.swarm_account = None
        args.model = None
        with pytest.raises(SystemExit, match="only supported for Claude and Codex"):
            cmd_hire(args)
        mock_spawn.assert_not_called()

    @staticmethod
    def _skill_dir(tmp_path):
        d = tmp_path / "myskill"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: myskill\ndescription: x\n---\nbody\n")
        return d

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/eval/dir"))
    def test_with_skill_wires_plugin_dir(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, mock_send, tmp_path,
    ):
        """--with-skill on a claude clean-room cook reaches spawn_window as a
        validated absolute path in plugin_dirs (→ --plugin-dir)."""
        mock_state.return_value = tmp_path
        skill = self._skill_dir(tmp_path)
        args = MagicMock()
        args.kitchen = "risotto"; args.name = "eval1"; args.backend = "claude"
        args.project = None; args.role = None; args.effort = None; args.clean_room = True
        args.with_skill = [str(skill)]; args.slot = None; args.swarm_account = None
        args.model = None  # a Claude hire passes the guard; assert model isn't accidentally forwarded
        cmd_hire(args)
        assert mock_spawn.call_args.kwargs["plugin_dirs"] == [str(skill.resolve())]
        assert mock_spawn.call_args.kwargs["model"] is None

    @patch("claude_kitchen.cli.spawn_window")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/eval/dir"))
    def test_with_skill_requires_clean_room(
        self, mock_rp, mock_rk, mock_state, mock_spawn, tmp_path,
    ):
        mock_state.return_value = tmp_path
        skill = self._skill_dir(tmp_path)
        args = MagicMock()
        args.kitchen = "risotto"; args.name = "x"; args.backend = "claude"
        args.project = None; args.role = None; args.effort = None; args.clean_room = False
        args.with_skill = [str(skill)]; args.slot = None; args.swarm_account = None
        with pytest.raises(SystemExit, match="requires --clean-room"):
            cmd_hire(args)
        mock_spawn.assert_not_called()

    @patch("claude_kitchen.cli.spawn_window")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/eval/dir"))
    def test_with_skill_non_claude_fails_loud(
        self, mock_rp, mock_rk, mock_state, mock_spawn, tmp_path,
    ):
        mock_state.return_value = tmp_path
        skill = self._skill_dir(tmp_path)
        args = MagicMock()
        args.kitchen = "risotto"; args.name = "x"; args.backend = "codex"
        args.project = None; args.role = None; args.effort = None; args.clean_room = True
        args.with_skill = [str(skill)]; args.slot = None; args.swarm_account = None
        with pytest.raises(SystemExit, match="not yet supported"):
            cmd_hire(args)
        mock_spawn.assert_not_called()

    @patch("claude_kitchen.cli.spawn_window")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/eval/dir"))
    def test_with_skill_bad_path_fails_clearly(
        self, mock_rp, mock_rk, mock_state, mock_spawn, tmp_path,
    ):
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"; args.name = "x"; args.backend = "claude"
        args.project = None; args.role = None; args.effort = None; args.clean_room = True
        args.with_skill = [str(tmp_path / "does-not-exist")]; args.slot = None; args.swarm_account = None
        with pytest.raises(SystemExit, match="not a directory"):
            cmd_hire(args)
        mock_spawn.assert_not_called()

    def test_validate_skill_path_rejects_plain_dir(self, tmp_path):
        """An existing dir that's neither a skill nor a plugin dir fails clearly."""
        from claude_kitchen.cli import _validate_skill_path
        d = tmp_path / "notaskill"; d.mkdir()
        with pytest.raises(SystemExit, match="not a skill or plugin dir"):
            _validate_skill_path(str(d))

    def test_validate_skill_path_accepts_plugin_dir(self, tmp_path):
        """A .claude-plugin/plugin.json dir is an accepted shape (→ abs path)."""
        from claude_kitchen.cli import _validate_skill_path
        d = tmp_path / "plug"
        (d / ".claude-plugin").mkdir(parents=True)
        (d / ".claude-plugin" / "plugin.json").write_text("{}")
        assert _validate_skill_path(str(d)) == str(d.resolve())


class TestCmdSuspend:
    """`suspend` kills the kitchen's own tmux server and touches NOTHING on
    disk — it is `close` minus the destruction."""

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.kill_server")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_kills_only_that_kitchens_server(self, mock_rk, mock_kill, mock_has, capsys):
        from claude_kitchen.cli import cmd_suspend
        cmd_suspend(MagicMock(kitchen="risotto"))
        mock_kill.assert_called_once_with("risotto")

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_leaves_every_byte_of_state_alone(self, mock_rk, mock_tmux, mock_has, tmp_path):
        """The contrast with `close`, which unlinks kitchen.json and rmtrees
        notes/ + cooks/. Nothing here may be removed or rewritten."""
        from claude_kitchen.cli import cmd_suspend
        (tmp_path / "cooks").mkdir()
        (tmp_path / "cooks" / "eng.json").write_text('{"status":"idle"}')
        (tmp_path / "notes").mkdir()
        (tmp_path / "notes" / "log.md").write_text("# Log\n")
        (tmp_path / "kitchen.json").write_text('{"source":"/x"}')
        before = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file()}
        with patch("claude_kitchen.cli.state_dir", return_value=tmp_path):
            cmd_suspend(MagicMock(kitchen="risotto"))
        after = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file()}
        assert after == before

    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_already_suspended_fails_clearly(self, mock_rk, mock_tmux, mock_has):
        from claude_kitchen.cli import cmd_suspend
        with pytest.raises(SystemExit, match="already suspended"):
            cmd_suspend(MagicMock(kitchen="risotto"))
        mock_tmux.assert_not_called()

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_prints_the_socket_qualified_attach_command(
            self, mock_rk, mock_tmux, mock_has, capsys):
        from claude_kitchen.cli import cmd_suspend
        cmd_suspend(MagicMock(kitchen="risotto"))
        out = capsys.readouterr().out
        assert "tmux -L ck-risotto attach" in out
        assert "kitchen open --resume risotto" in out


class TestOpenDoesNotSweepCooksItCannotSee:
    """A kitchen whose session this tmux server can't see — suspended, or alive
    on another socket — must NOT have its cook records deleted. Every record
    looks orphaned when the session had to be created, and that is precisely
    the case where the cooks may still be running (or where the records are the
    memory `suspend` promised to keep)."""

    @pytest.mark.parametrize("session_up", [False, True])
    def test_resume_keeps_cook_records_for_an_unreachable_session(
            self, tmp_path, monkeypatch, session_up):
        """End to end through cmd_open --resume: cooks/*.json survives untouched,
        whether the session had to be created or a first reopen of a suspended
        kitchen already made it (its window list still has none of the cooks)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        project = tmp_path / "proj"
        project.mkdir()
        base = tmp_path / ".claude-kitchen" / "widget"
        (base / "cooks").mkdir(parents=True)
        (base / "cooks" / "eng.json").write_text('{"status":"working"}')
        (base / "kitchen.json").write_text(json.dumps(
            {"source": str(project), "slug": "widget", "sous_session_id": "sid-1"}) + "\n")
        args = MagicMock(name_=None, project=str(project), resume=True,
                         worktree_path=None)
        args.name = "widget"
        with patch("claude_kitchen.cli.has_session", return_value=session_up), \
             patch("claude_kitchen.cli.list_windows", return_value=["_placeholder"]), \
             patch("claude_kitchen.cli.tmux") as mock_tmux, \
             patch("claude_kitchen.cli.project_slug", return_value="widget"), \
             patch("claude_kitchen.cli.namespaced", return_value="widget"), \
             patch("claude_kitchen.cli.spawn_sous") as mock_spawn:
            mock_tmux.return_value = MagicMock(returncode=0)
            cmd_open(args)
        assert (base / "cooks" / "eng.json").read_text() == '{"status":"working"}'
        assert mock_spawn.call_args.kwargs["resume_session_id"] == "sid-1"

    def test_resume_uses_host_session_when_container_section_is_removed(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        project = tmp_path / "proj"
        (project / ".kitchen").mkdir(parents=True)
        (project / ".kitchen" / "container.toml").write_text("# disabled\n")
        base = tmp_path / ".claude-kitchen" / "widget"
        base.mkdir(parents=True)
        (base / "kitchen.json").write_text(json.dumps({
            "source": str(project), "slug": "widget",
            "sous_container": True, "sous_session_id": "host-session",
        }) + "\n")
        (base / "container-session.json").write_text(
            '{"sous_session_id":"container-session"}\n'
        )
        args = MagicMock(name_=None, project=str(project), resume=True,
                         worktree_path=None)
        args.name = "widget"
        with patch("claude_kitchen.cli.has_session", return_value=False), \
             patch("claude_kitchen.cli.tmux", return_value=MagicMock(returncode=0)), \
             patch("claude_kitchen.cli.project_slug", return_value="widget"), \
             patch("claude_kitchen.cli.namespaced", return_value="widget"), \
             patch("claude_kitchen.cli.spawn_sous") as mock_spawn:
            cmd_open(args)

        assert mock_spawn.call_args.kwargs["resume_session_id"] == "host-session"


class TestSecondOpenIsANoOp:
    """`kitchen open` on a kitchen whose sous is already running must change
    NOTHING. The guard lives in spawn_sous, which is the last statement of
    cmd_open, so a duplicate open used to rewrite kitchen.json and
    kitchen-mcp.json and stand up a whole tmux server before aborting with
    "Sous chef already running". An aborted command must not mutate."""

    @patch("claude_kitchen.spawn.os.execvp")
    def _run_second_open(self, has_session_value, mock_exec, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        base = tmp_path / ".claude-kitchen" / "widget"
        (base / "cooks").mkdir(parents=True)

        kitchen_json = base / "kitchen.json"
        mcp_json = base / "kitchen-mcp.json"
        cook_json = base / "cooks" / "eng.json"
        # indent=2 so cmd_open's compact rewrite is detectable byte-wise; a
        # re-serialised identical dict would otherwise compare equal.
        kitchen_json.write_text(json.dumps(
            {"source": str(project), "slug": "widget", "sous_session_id": "sid-1"},
            indent=2) + "\n")
        mcp_json.write_text('{"sentinel": "untouched"}\n')
        cook_json.write_text('{"status":"working"}')
        # A sous that is genuinely alive — os.kill(pid, 0) succeeds on us.
        (base / "sous.pid").write_text(str(os.getpid()))

        before = (kitchen_json.read_text(), mcp_json.read_text(), cook_json.read_text())

        args = MagicMock(project=str(project), resume=False,
                         worktree_path=None)
        args.name = "widget"
        with patch("claude_kitchen.cli.has_session", return_value=has_session_value), \
             patch("claude_kitchen.cli.tmux") as mock_tmux, \
             patch("claude_kitchen.cli.project_slug", return_value="widget"), \
             patch("claude_kitchen.cli.namespaced", return_value="widget"), \
             patch("claude_kitchen.cli._sweep_cooks") as mock_sweep:
            mock_tmux.return_value = MagicMock(returncode=0)
            with pytest.raises(SystemExit) as exc:
                cmd_open(args)

        after = (kitchen_json.read_text(), mcp_json.read_text(), cook_json.read_text())
        return before, after, mock_tmux, mock_sweep, mock_exec, exc

    def test_second_open_writes_nothing_and_creates_no_server(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        before, after, mock_tmux, mock_sweep, mock_exec, exc = \
            self._run_second_open(False, tmp_path=tmp_path)

        assert "already running" in str(exc.value)
        assert after == before, "a refused open must not rewrite kitchen state"
        mock_tmux.assert_not_called(), "a refused open must not create a tmux server"
        mock_exec.assert_not_called()

    def test_second_open_does_not_sweep_cook_records(self, tmp_path, monkeypatch):
        """The dangerous branch: session reachable, so the pre-guard path would
        have reached _sweep_cooks."""
        monkeypatch.setenv("HOME", str(tmp_path))
        before, after, mock_tmux, mock_sweep, mock_exec, exc = \
            self._run_second_open(True, tmp_path=tmp_path)

        assert "already running" in str(exc.value)
        mock_sweep.assert_not_called(), "a refused open must never sweep cook records"
        assert after == before


class TestCmdClose:
    @pytest.mark.parametrize("containerized,expected", [
        (False, []), (True, [call("risotto")]),
    ])
    @patch("claude_kitchen.container.remove")
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_close_removes_only_recorded_container(
            self, mock_rk, mock_state, mock_tmux, mock_remove, tmp_path,
            containerized, expected):
        mock_state.return_value = tmp_path
        (tmp_path / "kitchen.json").write_text(json.dumps({
            "source": str(tmp_path), "sous_container": containerized,
        }))

        cmd_close(argparse.Namespace(kitchen="risotto", keep_worktree=False, force=False))

        assert mock_remove.call_args_list == expected

    @patch("claude_kitchen.container.remove", side_effect=RuntimeError("docker unavailable"))
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_failed_container_removal_keeps_cleanup_marker(
            self, mock_rk, mock_state, mock_tmux, mock_remove, tmp_path):
        mock_state.return_value = tmp_path
        (tmp_path / "kitchen.json").write_text(json.dumps({
            "source": str(tmp_path), "sous_container": True,
        }))

        with pytest.raises(RuntimeError, match="docker unavailable"):
            cmd_close(argparse.Namespace(kitchen="risotto", keep_worktree=False, force=False))

        assert (tmp_path / "kitchen.json").exists()

    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_removes_cooks_dir(self, mock_rk, mock_state, mock_tmux, tmp_path):
        mock_state.return_value = tmp_path
        mock_tmux.return_value = MagicMock(returncode=0)
        cooks = tmp_path / "cooks"
        cooks.mkdir()
        (cooks / "eng.json").write_text("{}")
        # clean-room codex per-cook homes (containing copied auth.json) must
        # also be wiped on close
        codex_home = tmp_path / "codex-home" / "eval1"
        codex_home.mkdir(parents=True)
        (codex_home / "auth.json").write_text("{}")
        # both the renamed config AND any legacy .mcp.json must be cleaned up
        # on close (no cook-discoverable leftover under base/)
        mcp_config = tmp_path / "kitchen-mcp.json"
        mcp_config.write_text("{}")
        legacy_config = tmp_path / ".mcp.json"
        legacy_config.write_text("{}")
        args = MagicMock()
        args.kitchen = "risotto"
        cmd_close(args)
        assert not cooks.exists()
        assert not (tmp_path / "codex-home").exists()
        assert not mcp_config.exists()
        assert not legacy_config.exists()

    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_clock_out_removes_codex_home(self, mock_rk, mock_state, mock_tmux, tmp_path):
        """clock-out tears down the cook's per-cook CODEX_HOME (and its auth.json)."""
        from claude_kitchen.cli import cmd_clock_out
        mock_state.return_value = tmp_path
        mock_tmux.return_value = MagicMock(returncode=0)
        (tmp_path / "cooks").mkdir()
        (tmp_path / "cooks" / "eval1.json").write_text("{}")
        codex_home = tmp_path / "codex-home" / "eval1"
        codex_home.mkdir(parents=True)
        (codex_home / "auth.json").write_text("{}")
        args = MagicMock()
        args.kitchen = "risotto"
        args.cook = "eval1"
        cmd_clock_out(args)
        assert not codex_home.exists()
        assert not (tmp_path / "cooks" / "eval1.json").exists()


class TestArchiveCodexThread:
    """Departing codex cooks get `codex archive <thread>` so they drop out of
    the ChatGPT app's list. A stand-in `codex` on PATH records its argv and
    exits with the given code — real subprocess, real status files."""

    def _fake_codex(self, tmp_path, monkeypatch, exit_code=0):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "codex-argv.log"
        (bin_dir / "codex").write_text(
            f"#!/bin/sh\necho \"$@\" >> {log}\n"
            f"[ {exit_code} -eq 0 ] || echo 'Error: failed to archive session' >&2\nexit {exit_code}\n")
        (bin_dir / "codex").chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
        return log

    def _cook(self, base, name, backend, session_id="01a141ca-d6db-7900-9282-ba3d37c09029"):
        write_status(base, name, {"status": "idle", "agent": name,
                                  "backend": backend, "session_id": session_id})

    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_clock_out_archives_codex_thread(self, mock_rk, mock_tmux, tmp_path, monkeypatch):
        log = self._fake_codex(tmp_path, monkeypatch)
        monkeypatch.setattr("claude_kitchen.cli.state_dir", lambda k: tmp_path)
        self._cook(tmp_path, "rev", "codex")
        cmd_clock_out(argparse.Namespace(kitchen="risotto", cook="rev"))
        assert log.read_text() == "archive 01a141ca-d6db-7900-9282-ba3d37c09029\n"
        assert not (tmp_path / "cooks" / "rev.json").exists()

    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_archive_failure_is_one_line_and_not_fatal(self, mock_rk, mock_tmux, tmp_path, monkeypatch, capsys):
        self._fake_codex(tmp_path, monkeypatch, exit_code=1)
        monkeypatch.setattr("claude_kitchen.cli.state_dir", lambda k: tmp_path)
        self._cook(tmp_path, "rev", "codex")
        cmd_clock_out(argparse.Namespace(kitchen="risotto", cook="rev"))
        out = capsys.readouterr()
        assert out.err.count("\n") == 1
        assert "rev: Codex thread 01a141ca-d6db-7900-9282-ba3d37c09029 not archived" in out.err
        assert "failed to archive session" in out.err
        assert "rev has clocked out." in out.out

    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_missing_codex_binary_is_one_line_and_not_fatal(self, mock_rk, mock_tmux, tmp_path, monkeypatch, capsys):
        (tmp_path / "empty").mkdir()
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.setattr("claude_kitchen.cli.state_dir", lambda k: tmp_path)
        self._cook(tmp_path, "rev", "codex")
        cmd_clock_out(argparse.Namespace(kitchen="risotto", cook="rev"))
        out = capsys.readouterr()
        assert out.err.count("\n") == 1
        assert "rev: Codex thread 01a141ca-d6db-7900-9282-ba3d37c09029 not archived (" in out.err
        assert "rev has clocked out." in out.out

    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_close_archives_only_shared_home_codex_cooks(self, mock_rk, mock_tmux, tmp_path, monkeypatch):
        log = self._fake_codex(tmp_path, monkeypatch)
        monkeypatch.setattr("claude_kitchen.cli.state_dir", lambda k: tmp_path)
        self._cook(tmp_path, "rev", "codex", "aaaaaaaa-0000-0000-0000-000000000001")
        self._cook(tmp_path, "rev2", "codex", "aaaaaaaa-0000-0000-0000-000000000002")
        self._cook(tmp_path, "eng", "claude", "bbbbbbbb-0000-0000-0000-000000000000")
        self._cook(tmp_path, "eval1", "codex", "cccccccc-0000-0000-0000-000000000000")
        (tmp_path / "codex-home" / "eval1").mkdir(parents=True)  # clean-room
        cmd_close(argparse.Namespace(kitchen="risotto", keep_worktree=False, force=False))
        assert log.read_text().splitlines() == [
            "archive aaaaaaaa-0000-0000-0000-000000000001",
            "archive aaaaaaaa-0000-0000-0000-000000000002",
        ]


class TestCmdCloseWipesNotesNotWiki:
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_close_removes_notes_dir(self, mock_rk, mock_state, mock_tmux, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        base = tmp_path / ".claude-kitchen" / "risotto"
        base.mkdir(parents=True)
        mock_state.return_value = base
        mock_tmux.return_value = MagicMock(returncode=0)

        notes = base / "notes"
        notes.mkdir()
        (notes / "log.md").write_text("stuff")
        wiki = tmp_path / ".claude-kitchen" / "projects" / "acme-widget" / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "mistakes.md").write_text("table")

        args = MagicMock()
        args.kitchen = "risotto"
        args.force = False
        cmd_close(args)

        assert not notes.exists(), "notes/ should be wiped"
        assert (wiki / "mistakes.md").exists(), "project wiki must persist"


class TestCmdSetupExit:
    @patch("claude_kitchen.cli.subprocess.run")
    def test_exits_non_zero_when_claude_missing(self, mock_run, monkeypatch, tmp_path):
        mock_run.return_value = MagicMock(returncode=127, stdout="", stderr="not found")
        monkeypatch.setenv("HOME", str(tmp_path))
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(
            '{"hooks":{"Stop":[{"hooks":[{"command":"kitchen hook"}]}]}}'
        )
        from claude_kitchen.cli import cmd_setup
        with pytest.raises(SystemExit) as exc:
            cmd_setup(MagicMock())
        assert exc.value.code == 1


class TestCmdSetupHooks:
    @patch("claude_kitchen.cli.subprocess.run")
    def test_takes_over_its_hooks_without_clobbering(self, mock_run, monkeypatch, tmp_path, capsys):
        """kitchen setup owns its hook entries: the old ~/.claude/hooks copy of a hook
        it ships is dropped, each owned entry appears exactly once, and everything
        else (keys, other hooks, a hook sharing a group with an old one) survives."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir()
        from claude_kitchen.cli import HOOKS as HOOKS_DIR
        old = lambda name: {"type": "command", "command": f"$HOME/.claude/hooks/{name}"}
        mine = {"type": "command", "command": "$HOME/bin/my-prompt-hook"}
        gate = {"type": "command", "command": "$HOME/.claude/hooks/knightwatch-inflight-push-gate.py"}  # not kitchen's
        original = {
            "model": "opus",
            "permissions": {"allow": ["Bash(ls:*)", "mcp__playwright__browser_snapshot"]},
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "kitchen hook"}]}],
                "UserPromptSubmit": [{"hooks": [old("capture-prompt.sh"), mine]}],
                "PreToolUse": [{"matcher": "Bash", "hooks": [old("roborev-pre-push-gate.py")]},
                               # the legacy roborev installer's copy (XDG-relocatable dir)
                               {"matcher": "Bash", "hooks": [{"type": "command", "command": "/home/u/xdg/roborev/claude-hooks/roborev-pre-checkout-gate.py"}]},
                               {"matcher": "Bash", "hooks": [gate]},
                               # the same canonical entry twice in one group: the second copy goes
                               {"matcher": "Bash", "hooks": [{"type": "command", "command": str(HOOKS_DIR / "roborev-pre-commit-context.py")}] * 2},
                               # kitchen's own path with the timeout missing: replaced by the canonical entry
                               {"matcher": "Bash", "hooks": [{"type": "command", "command": str(HOOKS_DIR / "roborev-pre-push-gate.py")}]}],
            },
        }
        settings.write_text(json.dumps(original))
        settings.chmod(0o640)
        from claude_kitchen.cli import cmd_setup, HOOKS

        for _ in range(2):
            with pytest.raises(SystemExit):  # superpowers is absent from this HOME
                cmd_setup(MagicMock())

        data = json.loads(settings.read_text())
        commands = [h["command"] for groups in data["hooks"].values() for g in groups for h in g["hooks"]]
        assert data["model"] == "opus"
        assert not any(c.startswith("$HOME/.claude/hooks/") and "knightwatch" not in c for c in commands)
        assert not any("/roborev/claude-hooks/" in c for c in commands)
        assert mine["command"] in commands and gate["command"] in commands
        for name in ("capture-prompt.sh", "capture-answer.sh", "roborev-pre-commit-context.py",
                     "roborev-pre-push-gate.py", "roborev-pre-checkout-gate.py"):
            assert commands.count(str(HOOKS / name)) == 1, name
        assert commands.count("kitchen hook") == 3  # Stop, StopFailure + UserPromptSubmit
        push = [h for g in data["hooks"]["PreToolUse"] for h in g["hooks"] if h["command"].endswith("roborev-pre-push-gate.py")]
        assert push == [{"type": "command", "command": str(HOOKS / "roborev-pre-push-gate.py"), "timeout": 660}]
        assert settings.stat().st_mode & 0o777 == 0o640
        backups = list(settings.parent.glob("settings.json.bak-kitchen-*"))
        assert len(backups) == 1 and json.loads(backups[0].read_text()) == original  # run 2 changed nothing
        assert data["permissions"]["allow"] == [
            "Bash(ls:*)", "mcp__playwright__browser_snapshot", "mcp__playwright__browser_take_screenshot"]
        out = capsys.readouterr().out
        assert "-capture-prompt.sh" in out and "-roborev-pre-push-gate.py" in out


class TestCmdSetupStatusline:
    """No statusLine: the sous runs kitchen's own. A statusLine of the user's own
    is advised to embed the kitchen segment and the usage writer. None block setup."""

    def _base_settings(self):
        return {
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "kitchen hook"}]}],
                "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "kitchen hook"}]}],
            }
        }

    def _prep_home(self, tmp_path, settings_obj):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        if settings_obj is not None:
            (claude_dir / "settings.json").write_text(json.dumps(settings_obj))
        sp = tmp_path / ".claude" / "plugins" / "cache" / "superpowers-marketplace" / "superpowers"
        sp.mkdir(parents=True)

    @patch("claude_kitchen.cli.subprocess.run")
    def test_missing_settings_file_advisory(self, mock_run, monkeypatch, tmp_path, capsys):
        """No settings.json → the sous runs kitchen's own statusline."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        from claude_kitchen.cli import cmd_setup
        with pytest.raises(SystemExit):
            cmd_setup(MagicMock())
        out = capsys.readouterr().out
        assert "the sous runs kitchen's own" in out

    @patch("claude_kitchen.cli.subprocess.run")
    def test_statusline_key_missing_advisory_only(self, mock_run, monkeypatch, tmp_path, capsys):
        """Valid settings, no statusLine key → kitchen's own, and setup exits zero."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        self._prep_home(tmp_path, self._base_settings())

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())
        out = capsys.readouterr().out
        assert "the sous runs kitchen's own" in out
        assert "🍳 All set" in out

    @pytest.mark.parametrize("exists", [False, True], ids=["missing", "existing"])
    @patch("claude_kitchen.cli.subprocess.run")
    def test_custom_statusline_embed_advice(self, mock_run, monkeypatch, tmp_path, capsys, exists):
        """A statusline of the user's own, even one whose script is missing, stays
        theirs: setup advises embedding the kitchen segment and the usage writer."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        script = tmp_path / "custom.sh"
        if exists:
            script.write_text("#!/bin/sh\necho hi\n")
            script.chmod(0o755)
        self._prep_home(tmp_path, {**self._base_settings(), "statusLine": {"type": "command", "command": str(script)}})

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())
        out = capsys.readouterr().out
        assert all(s in out for s in ("no kitchen segment detected", "$(kitchen statusline-segment)",
                                      "rate_limits writer", "🍳 All set"))

    @patch("claude_kitchen.cli.subprocess.run")
    def test_statusline_green_when_script_embeds_segment(self, mock_run, monkeypatch, tmp_path, capsys):
        """Script whose text references `kitchen statusline-segment` → green."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        script = tmp_path / ".claude" / "statusline-command.sh"
        settings = self._base_settings()
        settings["statusLine"] = {"type": "command", "command": f"bash {script}"}
        self._prep_home(tmp_path, settings)
        script.write_text("#!/bin/sh\n$(kitchen statusline-segment)\n")
        script.chmod(0o755)

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())
        out = capsys.readouterr().out
        assert "kitchen segment active" in out
        assert "not configured" not in out
        assert "no kitchen segment" not in out

    @patch("claude_kitchen.cli.subprocess.run")
    def test_statusline_green_when_command_is_segment_directly(self, mock_run, monkeypatch, tmp_path, capsys):
        """Minimal setup: `command: "kitchen statusline-segment"` → green."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        settings = self._base_settings()
        settings["statusLine"] = {"type": "command", "command": "kitchen statusline-segment"}
        self._prep_home(tmp_path, settings)

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())
        out = capsys.readouterr().out
        assert "kitchen segment active" in out

    def test_packaged_script_exists_and_is_executable(self):
        """Guard against the .sh file not shipping with the package."""
        from claude_kitchen.cli import _PKG_DIR
        script = _PKG_DIR / "statusline-command.sh"
        assert script.exists(), f"Packaged statusline missing at {script}"
        assert script.stat().st_mode & 0o111, "Packaged statusline should be executable"

    @pytest.mark.parametrize("rate_limits,kept", [
        ({"five_hour": {"used_percentage": 12.0, "resets_at": 1763600000},
          "seven_day": {"used_percentage": 83.0, "resets_at": 1763629200}, "spend_limit": {"resets_at": 1}},
         {"five_hour": {"used_percentage": 12.0, "resets_at": 1763600000},
          "seven_day": {"used_percentage": 83.0, "resets_at": 1763629200}}),
        ({"five_hour": {"used_percentage": 5.0, "resets_at": 1763600000}, "seven_day": None},
         {"five_hour": {"used_percentage": 5.0, "resets_at": 1763600000}}),
        (None, None)])
    def test_packaged_script_keeps_the_plans_usage(self, tmp_path, rate_limits, kept):
        """Each reply's statusline payload leaves ~/.claude/usage/rate_limits.json:
        the weekly and 5-hour windows Claude passed, and when; nothing without them."""
        from claude_kitchen.cli import _PKG_DIR
        payload = {"model": {"id": "claude-opus-5-5"}, "workspace": {"current_dir": str(tmp_path)},
                   **({"rate_limits": rate_limits} if rate_limits else {})}
        subprocess.run(["bash", str(_PKG_DIR / "statusline-command.sh")], input=json.dumps(payload), text=True,
                       capture_output=True, env={**os.environ, "HOME": str(tmp_path)})
        usage = tmp_path / ".claude" / "usage" / "rate_limits.json"
        if kept is None:
            assert not usage.exists()
            return
        got = json.loads(usage.read_text())
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", got.pop("updated_at"))
        assert got == kept

    def test_packaged_script_delegates_to_segment_cli(self):
        """The packaged script must reference `kitchen statusline-segment` —
        otherwise installing it wouldn't satisfy the green branch of the check."""
        from claude_kitchen.cli import _PKG_DIR
        body = (_PKG_DIR / "statusline-command.sh").read_text()
        assert "kitchen statusline-segment" in body


class TestCmdSetupRootMcp:
    """`kitchen setup` auto-removes a stray state-root .mcp.json (§Design.4)."""

    def _green_home(self, tmp_path):
        # Minimal env so cmd_setup reaches the root-.mcp.json check and exits 0.
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(json.dumps({
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "kitchen hook"}]}],
                "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "kitchen hook"}]}],
            }
        }))
        (tmp_path / ".claude" / "plugins" / "cache" / "superpowers-marketplace" / "superpowers").mkdir(parents=True)

    @patch("claude_kitchen.cli.subprocess.run")
    def test_removes_stray_root_mcp_json(self, mock_run, monkeypatch, tmp_path, capsys):
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        self._green_home(tmp_path)
        root_mcp = tmp_path / ".claude-kitchen" / ".mcp.json"
        root_mcp.parent.mkdir(parents=True)
        root_mcp.write_text('{"mcpServers": {}}')

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())

        assert not root_mcp.exists(), "stray root .mcp.json must be removed"
        out = capsys.readouterr().out
        assert "Removed stray root-level MCP config" in out
        assert str(root_mcp) in out

    @patch("claude_kitchen.cli.subprocess.run")
    def test_noop_when_absent_idempotent(self, mock_run, monkeypatch, tmp_path, capsys):
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        self._green_home(tmp_path)
        (tmp_path / ".claude-kitchen").mkdir(parents=True)

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())  # no .mcp.json present
        cmd_setup(MagicMock())  # second run — still a clean no-op
        out = capsys.readouterr().out
        assert "Removed stray root-level MCP config" not in out
        assert not (tmp_path / ".claude-kitchen" / ".mcp.json").exists()

    @patch("claude_kitchen.cli.subprocess.run")
    def test_does_not_touch_per_kitchen_config(self, mock_run, monkeypatch, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        self._green_home(tmp_path)
        root = tmp_path / ".claude-kitchen"
        root.mkdir(parents=True)
        (root / ".mcp.json").write_text("{}")
        # files that must survive: root-level kitchen-mcp.json and a per-kitchen
        # config under a kitchen dir
        root_kitchen_cfg = root / "kitchen-mcp.json"
        root_kitchen_cfg.write_text("{}")
        per_kitchen = root / "risotto"
        per_kitchen.mkdir()
        per_kitchen_cfg = per_kitchen / "kitchen-mcp.json"
        per_kitchen_cfg.write_text("{}")

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())

        assert not (root / ".mcp.json").exists(), "root .mcp.json removed"
        assert root_kitchen_cfg.exists(), "root kitchen-mcp.json must be untouched"
        assert per_kitchen_cfg.exists(), "per-kitchen config must be untouched"


class TestCmdSetupSuperpowers:
    """superpowers installs from either marketplace; setup accepts both."""

    @pytest.mark.parametrize("marketplace", ["superpowers-marketplace", "claude-plugins-official"])
    @patch("claude_kitchen.cli.subprocess.run")
    def test_either_marketplace_detected(self, mock_run, marketplace, monkeypatch, tmp_path, capsys):
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        sp = tmp_path / ".claude" / "plugins" / "cache" / marketplace / "superpowers"
        sp.mkdir(parents=True)
        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())  # setup installs the hooks itself, so it passes
        assert f"✅ superpowers plugin installed ({sp})" in capsys.readouterr().out

    @patch("claude_kitchen.cli.subprocess.run")
    def test_missing_fails(self, mock_run, monkeypatch, tmp_path, capsys):
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        from claude_kitchen.cli import cmd_setup
        with pytest.raises(SystemExit):
            cmd_setup(MagicMock())
        out = capsys.readouterr().out
        assert "❌ superpowers plugin not found" in out
        assert "superpowers-marketplace" in out and "claude-plugins-official" in out


class TestCmdStatuslineSegment:
    """`kitchen statusline-segment` soft-resolves the current kitchen, prints
    one line, and is silent when outside any kitchen.

    The count is scoped to live tmux windows (the same source brigade uses),
    NOT a glob over cooks/*.json — so it describes exactly the kitchen the
    attach target points at and isn't inflated by orphaned state files left
    behind when a cook's window dies. Because it's wired into the user's
    prompt, it must degrade to empty/partial output and never raise."""

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status")
    @patch("claude_kitchen.cli.list_windows")
    @patch("claude_kitchen.cli.list_kitchens", return_value=[])
    def test_no_kitchen_prints_nothing(self, mock_ls, mock_win, mock_status, mock_has, monkeypatch, capsys):
        monkeypatch.delenv("AGENT_KITCHEN", raising=False)
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        assert capsys.readouterr().out == ""

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status")
    @patch("claude_kitchen.cli.list_windows")
    @patch("claude_kitchen.cli.list_kitchens", return_value=["a", "b"])
    def test_ambiguous_without_agent_session_is_silent(self, mock_ls, mock_win, mock_status, mock_has, monkeypatch, capsys):
        monkeypatch.delenv("AGENT_KITCHEN", raising=False)
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        assert capsys.readouterr().out == ""

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status")
    @patch("claude_kitchen.cli.list_windows", return_value=["a", "b", "c", "d"])
    def test_with_agent_session_prints_attach_and_counts(
        self, mock_win, mock_status, mock_has, monkeypatch, capsys,
    ):
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        mock_status.side_effect = [
            {"status": "working"}, {"status": "booting"},
            {"status": "idle"}, {"status": "failed"},
        ]
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        out = capsys.readouterr().out.rstrip("\n")
        assert out == "[ tmux -L ck-risotto attach ]  [ 2/4 agents active ]"
        mock_win.assert_called_once_with("risotto", timeout=PROBE_TIMEOUT)

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status", return_value={"status": "working"})
    @patch("claude_kitchen.cli.list_windows", return_value=["cook0"])
    @patch("claude_kitchen.cli.list_kitchens", return_value=["solo"])
    def test_single_session_without_agent_session_omits_attach_hint(
        self, mock_ls, mock_win, mock_status, mock_has, monkeypatch, capsys,
    ):
        """Called from outside sous (no AGENT_SESSION) but only one kitchen
        is running → segment still renders, but without the attach hint
        since the caller isn't in sous context."""
        monkeypatch.delenv("AGENT_KITCHEN", raising=False)
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        out = capsys.readouterr().out.rstrip("\n")
        assert out == "[ 1/1 agents active ]"

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status")
    @patch("claude_kitchen.cli.list_windows", return_value=[])
    def test_no_live_windows_reports_zero_over_zero(
        self, mock_win, mock_status, mock_has, monkeypatch, capsys,
    ):
        monkeypatch.setenv("AGENT_KITCHEN", "empty")
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        out = capsys.readouterr().out.rstrip("\n")
        assert out == "[ tmux -L ck-empty attach ]  [ 0/0 agents active ]"

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status")
    @patch("claude_kitchen.cli.list_windows", return_value=["live0", "live1"])
    def test_orphaned_state_files_do_not_inflate_count(
        self, mock_win, mock_status, mock_has, monkeypatch, tmp_path, capsys,
    ):
        """Regression: a kitchen with stale cooks/*.json for dead windows must
        still report only its LIVE cooks. Pre-fix this globbed every json file
        and rendered e.g. `5/18` for a 9-cook kitchen."""
        monkeypatch.setenv("AGENT_KITCHEN", "r")
        # State dir littered with 16 orphan files (no live window) — the
        # pre-fix glob would have counted all of them.
        with patch("claude_kitchen.cli.state_dir", return_value=tmp_path):
            cooks = tmp_path / "cooks"
            cooks.mkdir()
            for i in range(16):
                (cooks / f"orphan{i}.json").write_text(json.dumps({"status": "working"}))
            mock_status.side_effect = [{"status": "working"}, {"status": "idle"}]

            from claude_kitchen.cli import cmd_statusline_segment
            cmd_statusline_segment(MagicMock())
        out = capsys.readouterr().out.rstrip("\n")
        assert out == "[ tmux -L ck-r attach ]  [ 1/2 agents active ]"

    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.list_windows")
    def test_stale_agent_session_renders_nothing(
        self, mock_win, mock_has, monkeypatch, capsys,
    ):
        """Regression: a dead/closed session referenced by AGENT_SESSION must
        render empty and never raise — not an attach hint to a gone session,
        and not a CalledProcessError from list_windows(check=True)."""
        monkeypatch.setenv("AGENT_KITCHEN", "ghost")
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        assert capsys.readouterr().out == ""
        mock_win.assert_not_called()

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status")
    @patch("claude_kitchen.cli.list_windows",
           side_effect=subprocess.CalledProcessError(1, ["tmux", "list-windows"]))
    def test_session_closing_during_listing_does_not_raise(
        self, mock_win, mock_status, mock_has, monkeypatch, capsys,
    ):
        """Regression (TOCTOU): the session disappears between has_session and
        list_windows. Must degrade to 0/0, never propagate the error."""
        monkeypatch.setenv("AGENT_KITCHEN", "closing")
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        out = capsys.readouterr().out.rstrip("\n")
        assert out == "[ tmux -L ck-closing attach ]  [ 0/0 agents active ]"

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status")
    @patch("claude_kitchen.cli.list_windows", return_value=["ok", "broken"])
    def test_malformed_cook_json_counts_as_inactive(
        self, mock_win, mock_status, mock_has, monkeypatch, capsys,
    ):
        """Regression: a cook file that fails to parse is counted inactive (but
        still counted in the total), restoring the tolerance the old glob had.
        Pre-fix the unguarded read_status raised and the statusline threw."""
        monkeypatch.setenv("AGENT_KITCHEN", "r")
        mock_status.side_effect = [
            {"status": "working"},
            json.JSONDecodeError("Expecting value", "doc", 0),
        ]
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        out = capsys.readouterr().out.rstrip("\n")
        assert out == "[ tmux -L ck-r attach ]  [ 1/2 agents active ]"

    # ---- per-kitchen socket: the statusline must never stall or throw ----
    # Each kitchen now has its own tmux server, so "MY server is wedged" is a
    # reachable state for the very kitchen the statusline describes. At the
    # default 15s budget that would freeze the head chef's prompt on every
    # render; unguarded it would raise TimeoutExpired into it.

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status")
    @patch("claude_kitchen.cli.list_windows", return_value=["a"])
    def test_tmux_probes_are_bounded_by_probe_timeout(
        self, mock_win, mock_status, mock_has, monkeypatch, capsys,
    ):
        from claude_kitchen.cli import cmd_statusline_segment
        from claude_kitchen.tmux import PROBE_TIMEOUT
        monkeypatch.setenv("AGENT_KITCHEN", "r")
        cmd_statusline_segment(MagicMock())
        assert mock_has.call_args.kwargs["timeout"] == PROBE_TIMEOUT
        assert mock_win.call_args.kwargs["timeout"] == PROBE_TIMEOUT

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status")
    @patch("claude_kitchen.cli.list_windows",
           side_effect=subprocess.TimeoutExpired(cmd="tmux", timeout=2))
    def test_wedged_own_server_degrades_instead_of_raising(
        self, mock_win, mock_status, mock_has, monkeypatch, capsys,
    ):
        """This kitchen's server answers the probe and then stops answering.
        The segment must still render (0/0), not put a traceback in the prompt."""
        monkeypatch.setenv("AGENT_KITCHEN", "wedged")
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        out = capsys.readouterr().out.rstrip("\n")
        assert out == "[ tmux -L ck-wedged attach ]  [ 0/0 agents active ]"


class TestCmdRoles:
    def test_lists_roles_with_descriptions(self, capsys):
        from claude_kitchen.cli import cmd_roles
        cmd_roles(MagicMock())
        out = capsys.readouterr().out
        for r in ("_default", "eng", "reviewer", "qa"):
            assert r in out
        assert "implementer" in out  # from eng.md header
        assert "reviewer" in out


class TestBrigadeOutput:
    @patch("claude_kitchen.cli.list_kitchens", return_value=["risotto"])
    @patch("claude_kitchen.cli.list_windows", return_value=["cook1", "cook2"])
    @patch("claude_kitchen.cli.read_status")
    def test_brigade_shows_sous_and_cooks(self, mock_status, mock_win, mock_sess, capsys):
        mock_status.side_effect = [
            {"status": "working"},
            {"status": "idle", "summary": "tests pass"},  # summary must be dropped
        ]
        cmd_brigade(kitchen=None)
        out = capsys.readouterr().out
        assert "risotto" in out
        assert "cook1" in out
        assert "cook2" in out
        # Summary suffix removed (Chunk 4).
        assert "tests pass" not in out
        assert '-- "' not in out



def _kitchen_here(name, sous=None, **kj):
    """A kitchen on disk; `sous` is its sous's pid (os.getpid() for one that runs)."""
    d = Path.home() / ".claude-kitchen" / name
    d.mkdir(parents=True)
    (d / "kitchen.json").write_text(json.dumps(kj))
    if sous:
        (d / "sous.pid").write_text(str(sous))


def test_ls_marks_a_kitchen_whose_sous_exited_stopped_with_the_command_that_reopens_it(monkeypatch, capsys):
    """Its cooks' server outlives a sous that ran in a terminal since closed: attaching there
    opens an empty session, so ls marks it stopped and prints how to reopen it where it ran."""
    gone = subprocess.Popen(["true"])
    gone.wait()
    _kitchen_here("my-kitchen", sous=gone.pid, source="/home/you/plow/my-kitchen", sous_session_id="s1",
                  sous_launch={"argv": ["/home/you/.local/bin/kitchen", "open"], "cwd": "/home/you/plow/my-kitchen",
                               "socket": "", "pane": None})
    monkeypatch.setattr(sys, "argv", ["kitchen", "ls"])
    with patch("claude_kitchen.cli.has_session", return_value=True), \
            patch("claude_kitchen.cli.list_windows", return_value=[]):
        main()
    assert capsys.readouterr().out.splitlines() == [
        "my-kitchen  (stopped)", "  cd /home/you/plow/my-kitchen && kitchen open --resume"]


def test_ls_prints_each_running_kitchens_attach_command_to_copy_and_the_stopped_ones(monkeypatch, capsys):
    _kitchen_here("acme-cos", source="/home/you/plow/acme/cos")
    _kitchen_here("my-kitchen", sous=os.getpid())
    monkeypatch.setattr(sys, "argv", ["kitchen", "ls"])
    with patch("claude_kitchen.cli.has_session", side_effect=lambda k, timeout=None: k == "my-kitchen"), \
            patch("claude_kitchen.cli.list_windows", return_value=[]), \
            patch("claude_kitchen.cli._weekly_usage", return_value=None):
        main()
    assert capsys.readouterr().out.splitlines() == [
        "acme-cos  (stopped)", "  cd /home/you/plow/acme/cos && kitchen open", "kitchen attach my-kitchen"]


def _sous_tmux(kitchen):
    """A kitchen whose sous runs in its own tmux, as install.sh starts its agents
    (plow-<company>-<agent>); returns that socket and pane."""
    sock = os.path.join(os.environ["TMUX_TMPDIR"], "plow-acme-cos")
    subprocess.run(["tmux", "-S", sock, "new-session", "-d", "-s", "agent", "sleep", "30"], check=True)
    pane = subprocess.run(["tmux", "-S", sock, "list-panes", "-F", "#{pane_id}"],
                          capture_output=True, text=True, check=True).stdout.strip()
    _kitchen_here(kitchen, sous=os.getpid(), sous_launch={"socket": sock, "pane": pane})
    return sock, pane


def test_ls_prints_the_attach_command_of_a_suspended_kitchen_whose_sous_runs_on(monkeypatch, capsys):
    """`kitchen suspend` kills only the cooks' server: the sous's own tmux still answers,
    so ls prints the command that opens it, as attach does."""
    sock, _ = _sous_tmux("acme-cos")
    monkeypatch.setattr(sys, "argv", ["kitchen", "ls"])
    try:
        with patch("claude_kitchen.cli.has_session", return_value=False):
            main()
    finally:
        subprocess.run(["tmux", "-S", sock, "kill-server"], capture_output=True)
    assert capsys.readouterr().out.splitlines() == ["kitchen attach acme-cos"]


def test_attach_opens_the_sous_where_it_runs_else_the_kitchens_own_server(monkeypatch):
    """An install.sh agent's sous runs in its own tmux (plow-<company>-<agent>): attach
    opens that pane. Once that tmux is gone, attach opens the kitchen's server."""
    sock, pane = _sous_tmux("acme-cos")
    monkeypatch.setattr(sys, "argv", ["kitchen", "attach", "acme-cos"])
    try:
        with patch("claude_kitchen.cli.os.execvp", side_effect=SystemExit) as ex, \
                patch("claude_kitchen.cli.has_session", return_value=True):
            with pytest.raises(SystemExit):
                main()
            assert ex.call_args.args[1][1:] == ["-S", sock, "attach", "-t", pane]
            subprocess.run(["tmux", "-S", sock, "kill-server"], check=True)
            with pytest.raises(SystemExit):
                main()
            assert ex.call_args.args[1][1:] == ["-L", "ck-acme-cos", "attach"]
    finally:
        subprocess.run(["tmux", "-S", sock, "kill-server"], capture_output=True)


def test_attach_to_a_name_not_here_says_so(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["kitchen", "attach", "nope"])
    with patch("claude_kitchen.cli.has_session", return_value=False), \
            pytest.raises(SystemExit, match="kitchen ls"):
        main()



class TestAgySummary:
    """_agy_summary gates the gemini Stop notification, so it must NEVER raise.
    Regression: a non-string PLANNER_RESPONSE.content (schema drift) used to hit
    `.rstrip` and AttributeError straight past the OSError guard, so the hook
    failed to mark the cook idle or send the notification."""

    def _payload(self, tmp_path, lines):
        p = tmp_path / "transcript.jsonl"
        p.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
        return {"transcriptPath": str(p)}

    def test_non_string_content_does_not_raise(self, tmp_path):
        # list / dict content (not str). Buggy code: `.rstrip` -> AttributeError.
        payload = self._payload(tmp_path, [
            {"type": "PLANNER_RESPONSE", "content": ["a", "b"]},
            {"type": "PLANNER_RESPONSE", "content": {"x": 1}},
        ])
        assert _agy_summary(payload) == ""   # skipped, no raise

    def test_skips_non_string_keeps_last_valid_string(self, tmp_path):
        # A non-string entry interleaved between valid strings must be skipped,
        # keeping the last valid string (not crash before reaching it).
        payload = self._payload(tmp_path, [
            {"type": "PLANNER_RESPONSE", "content": "first valid"},
            {"type": "PLANNER_RESPONSE", "content": ["junk", 2]},
            {"type": "PLANNER_RESPONSE", "content": "last valid\n"},
        ])
        assert _agy_summary(payload) == "last valid"

    def test_well_formed_returns_last_nonempty_planner_response(self, tmp_path):
        # Sanity: normal transcript — empty placeholders filtered, last wins.
        payload = self._payload(tmp_path, [
            {"type": "USER_INPUT", "content": "ignored"},
            {"type": "PLANNER_RESPONSE", "content": ""},
            {"type": "PLANNER_RESPONSE", "content": "the answer\n"},
        ])
        assert _agy_summary(payload) == "the answer"


    def test_the_stop_hook_marks_the_cook_idle_and_notifies_without_ctx(self, tmp_path, monkeypatch):
        write_status(tmp_path, "gem", {"status": "working", "agent": "gem", "backend": "gemini"})
        payload = self._payload(tmp_path, [{"type": "PLANNER_RESPONSE", "content": "done\n"}])
        payload["conversationId"] = "conv-1"
        monkeypatch.setenv("AGENT_NAME", "gem")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        monkeypatch.setattr("sys.stdin", MagicMock(read=MagicMock(return_value=json.dumps(payload))))
        with patch("claude_kitchen.channel.send_to_socket") as push:
            main_argv = ["kitchen", "hook-agy"]
            monkeypatch.setattr("sys.argv", main_argv)
            main()
        status = json.loads((tmp_path / "cooks" / "gem.json").read_text())
        assert (status["status"], status["summary"], status["session_id"], status["backend"]) == \
            ("idle", "done", "conv-1", "gemini")
        assert push.call_args.args[1] == {"cook": "gem", "summary": "done", "ts": status["ts"]}


class TestTicketSous:
    """`kitchen ticket sous` through the real CLI parser and a real unix socket."""

    @pytest.fixture
    def kitchens(self, monkeypatch):
        # Under /tmp: a pytest tmp_path can overrun the AF_UNIX path limit.
        with tempfile.TemporaryDirectory(dir="/tmp") as home:
            monkeypatch.setenv("HOME", home)
            monkeypatch.setenv("AGENT_KITCHEN", "sender-main")
            monkeypatch.setattr("claude_kitchen.cli.has_session", lambda *a, **k: True)
            base = Path(home) / ".claude-kitchen" / "target-main"
            base.mkdir(parents=True)
            yield base

    def ticket(self, monkeypatch, *flags, priority=("--priority", "!prio:s $row")):
        monkeypatch.setattr(sys, "argv", ["kitchen", "ticket", "sous", "check the deploy", *priority, *flags])
        main()

    @pytest.mark.parametrize("priority, summary", [
        (("--priority", "!prio:s $row"), "check the deploy [priority: !prio:s $row]"),
        (("--dangerously-skip-priority",), "check the deploy"),
    ], ids=["priority", "skip"])
    def test_delivers_on_the_target_socket_labelled_with_the_senders_kitchen(self, kitchens, monkeypatch, capsys, priority, summary):
        received = []
        with socket.socket(socket.AF_UNIX) as srv:
            srv.bind(str(kitchens / "kitchen.sock"))
            srv.listen(1)

            def accept():
                conn, _ = srv.accept()
                with conn, conn.makefile() as lines:
                    received.append(json.loads(lines.readline()))

            listener = threading.Thread(target=accept)
            listener.start()
            with patch("claude_kitchen.cli.send_keys") as paste:
                self.ticket(monkeypatch, "--kitchen", "target-main", priority=priority)
            listener.join(timeout=2)

        assert received[0]["cook"] == "sender-main"
        assert received[0]["summary"] == summary
        paste.assert_not_called()  # the socket, never a pasted keystroke
        assert not (kitchens / "cooks").exists()
        assert (SKIP_PRIORITY_WARNING in capsys.readouterr().err) == ("--dangerously-skip-priority" in priority)

    @pytest.mark.parametrize("priority", [(), ("--priority", "p", "--dangerously-skip-priority")], ids=["neither", "both"])
    def test_ticket_without_exactly_one_priority_flag_is_refused(self, kitchens, monkeypatch, priority):
        with pytest.raises(SystemExit) as exit_:
            self.ticket(monkeypatch, "--kitchen", "target-main", priority=priority)
        assert exit_.value.code == 2

    @pytest.mark.parametrize("stale", [False, True], ids=["no-socket", "stale-socket"])
    def test_dead_target_exits_non_zero_and_writes_no_status(self, kitchens, monkeypatch, capsys, stale):
        if stale:
            with socket.socket(socket.AF_UNIX) as dead:
                dead.bind(str(kitchens / "kitchen.sock"))

        with pytest.raises(SystemExit) as exit_:
            self.ticket(monkeypatch, "--kitchen", "target-main")

        assert isinstance(exit_.value.code, str)  # a message exits 1
        assert "target-main" in exit_.value.code
        assert "Heard" not in capsys.readouterr().out
        assert not (kitchens / "cooks").exists()

    def test_unknown_kitchen_exits_non_zero_without_creating_it(self, kitchens, monkeypatch):
        with pytest.raises(SystemExit) as exit_:
            self.ticket(monkeypatch, "--kitchen", "nowhere")

        assert "nowhere" in exit_.value.code
        assert not (kitchens.parent / "nowhere").exists()

    def test_outside_a_sous_refuses_rather_than_send_an_unanswerable_ticket(self, kitchens, monkeypatch):
        monkeypatch.delenv("AGENT_KITCHEN")

        with pytest.raises(SystemExit) as exit_:
            self.ticket(monkeypatch, "--kitchen", "target-main")

        assert "AGENT_KITCHEN" in exit_.value.code


class TestLinkInstalls:
    def test_creates_retargets_skips_and_reruns_clean(self, tmp_path):
        from claude_kitchen.cli import _link_installs
        pkg = tmp_path / "pkg"
        for name in ("fresh", "moved", "mine", "theirs"):
            (pkg / "skills" / name).mkdir(parents=True)
        (pkg / "skills" / "README.md").write_text("not a skill")
        (pkg / "tc").mkdir()
        (pkg / "tc" / "tc").write_text("#!/bin/sh\n")
        home = tmp_path / "home"
        claude, agents, local_bin = home / ".claude" / "skills", home / ".agents" / "skills", home / ".local" / "bin"
        claude.mkdir(parents=True)
        cc = tmp_path / "services" / "code-config"
        (claude / "moved").symlink_to(cc / "claude" / "skills" / "moved")
        (claude / "mine").mkdir()  # a real directory: never touched
        (claude / "theirs").symlink_to(cc / "claude" / "other" / "theirs")  # code-config, but not its skills dir
        (claude / ".moved.kitchen-tmp").symlink_to(tmp_path / "stale")  # an interrupted run's leftover
        agents.mkdir(parents=True)
        (agents / "moved").symlink_to(cc / "codex" / "skills" / "moved")
        local_bin.mkdir(parents=True)
        (local_bin / "tc").symlink_to(cc / "tc")

        first = _link_installs(pkg, home)

        assert (claude / "fresh").resolve() == pkg / "skills" / "fresh"
        assert (agents / "fresh").resolve() == pkg / "skills" / "fresh"
        assert os.readlink(claude / "moved") == str(pkg / "skills" / "moved")
        assert os.readlink(agents / "moved") == str(pkg / "skills" / "moved")
        assert os.readlink(local_bin / "tc") == str(pkg / "tc" / "tc")
        assert (claude / "mine").is_dir() and not (claude / "mine").is_symlink()
        assert os.readlink(claude / "theirs") == str(cc / "claude" / "other" / "theirs")
        assert not (claude / "README.md").exists()
        assert sum(l.startswith("skipped") for l in first) == 2
        assert sum(l.startswith("retargeted") for l in first) == 3

        second = _link_installs(pkg, home)
        assert [l.split()[0] for l in second].count("ok") == 7
        assert sum(l.startswith("skipped") for l in second) == 2
        assert not list(claude.glob(".*kitchen-tmp"))


def test_install_claude_hooks_drops_a_duplicate_in_otherwise_canonical_settings(tmp_path):
    from claude_kitchen.cli import _install_claude_hooks, OWNED_HOOKS
    settings = tmp_path / "settings.json"
    _install_claude_hooks(settings)  # canonical install
    data = json.loads(settings.read_text())
    group = next(g for g in data["hooks"]["PreToolUse"] if g["hooks"][0]["command"].endswith("roborev-pre-commit-context.py"))
    group["hooks"] = group["hooks"] * 2  # the only defect: one entry twice
    settings.write_text(json.dumps(data))
    assert _install_claude_hooks(settings) == ["-roborev-pre-commit-context.py"]
    commands = [h["command"] for gs in json.loads(settings.read_text())["hooks"].values() for g in gs for h in g["hooks"]]
    assert len(commands) == len(OWNED_HOOKS)


def test_install_claude_hooks_removes_the_retired_consult_gate(tmp_path):
    """The consult gate moved into swarm: setup drops its hook, bare or wrapped in a kitchen-only guard,
    and leaves an unrelated hook in the same group alone."""
    from claude_kitchen.cli import _install_claude_hooks
    settings = tmp_path / "settings.json"
    other = {"type": "command", "command": "/x/my-swarm-logger"}
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "mcp__swarm.*__send", "hooks": [{"type": "command", "command": "kitchen consult-gate"}, other]},
        {"matcher": "mcp__swarm.*__send", "hooks": [{"type": "command", "command":
            'if [ -n "$AGENT_KITCHEN" ]; then exec /home/you/.local/bin/kitchen consult-gate; fi'}]}]}}))
    changed = _install_claude_hooks(settings)
    assert changed.count("-consult-gate") == 2
    commands = [h["command"] for gs in json.loads(settings.read_text())["hooks"].values() for g in gs for h in g["hooks"]]
    assert not [c for c in commands if "consult-gate" in c]
    assert other["command"] in commands


def test_confirm_dev_channels_answers_only_the_new_dialog():
    """A dead sous's leftover dialog text gets no Enter; the new dialog gets one, so `kitchen open` starts unattended."""
    import time
    from claude_kitchen.cli import confirm_dev_channels
    d = tempfile.mkdtemp(prefix="ck", dir="/tmp")  # short: a tmux socket path must fit sun_path
    sock, early, answer = f"{d}/s", Path(d) / "early", Path(d) / "answer"
    pane_script = (f"echo 'Loading development channels'; read -t 2 x && touch {early}; "
                   f"echo 'Loading development channels'; read line; echo answered > {answer}")
    subprocess.run(["tmux", "-S", sock, "new-session", "-d", "-s", "t", "bash", "-c", pane_script], check=True)
    try:
        pane = subprocess.run(["tmux", "-S", sock, "display", "-p", "-t", "t", "#{pane_id}"],
                              capture_output=True, text=True, check=True).stdout.strip()
        time.sleep(0.3)  # the leftover text is on screen before the watcher starts
        confirm_dev_channels(sock, pane)
        deadline = time.monotonic() + 10
        while not answer.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert answer.read_text().strip() == "answered"
        assert not early.exists()
    finally:
        subprocess.run(["tmux", "-S", sock, "kill-server"], check=False)


def test_windows_hook_commands_run_in_git_bash(monkeypatch):
    """Claude Code on Windows runs hooks in Git Bash: a quoted forward-slash path,
    and a .py on kitchen's own Python (Git Bash's python3 is the Store's stub)."""
    import claude_kitchen.cli as cli
    monkeypatch.setattr(cli, "WINDOWS", True)
    monkeypatch.setattr(cli, "HOOKS", Path("/x/hooks"))
    assert cli._hook("capture-prompt.sh") == '"/x/hooks/capture-prompt.sh"'
    assert cli._hook("roborev-pre-push-gate.py") == f'"{Path(sys.executable).as_posix()}" "/x/hooks/roborev-pre-push-gate.py"'


def test_windows_sous_is_told_what_runs_there(monkeypatch):
    """tc (so --slot) and `kitchen restart sous` don't run on Windows: the sous there is told what does instead."""
    import claude_kitchen.cli as cli
    unix = cli.sous_prompt()
    monkeypatch.setattr(cli, "WINDOWS", True)
    windows = cli.sous_prompt()
    assert windows.startswith(unix) and "## On Windows" not in unix
    extra = windows[len(unix):]
    assert "--slot" in extra and "--project" in extra and "kitchen open --resume" in extra


def test_windows_setup_retargets_a_skill_junction(tmp_path, monkeypatch):
    """A junction is a directory to Windows, so os.replace can't move a new link onto it: setup removes the old
    link (never what it points at) and makes it again, leaving no temporary link behind."""
    import claude_kitchen.cli as cli
    monkeypatch.setattr(cli, "WINDOWS", True)
    monkeypatch.setattr(cli, "_link", lambda dest, src: dest.symlink_to(src))  # a junction's stand-in here
    def replace(*a):
        raise PermissionError("[WinError 5] Access is denied")  # what Windows says moving onto a junction
    monkeypatch.setattr(cli.os, "replace", replace)
    monkeypatch.setattr(cli.os, "rmdir", os.unlink)  # rmdir removes a junction on Windows, as unlink a symlink here
    old = tmp_path / "code-config" / "skills" / "x"
    new = tmp_path / "kitchen" / "skills" / "x"
    old.mkdir(parents=True); new.mkdir(parents=True)
    dest = tmp_path / "home" / "x"
    dest.parent.mkdir(); dest.symlink_to(old)
    assert cli._place_link(new, dest, ("code-config/skills/x",)) == f"retargeted {dest} (was {old})"
    assert Path(os.readlink(dest)) == new and old.is_dir()
    assert [p.name for p in dest.parent.iterdir()] == ["x"]


def test_windows_hire_refuses_before_writing_anything(tmp_path, monkeypatch):
    """A cook kitchen can't launch on Windows is refused before its role or its 'booting' record is written."""
    import claude_kitchen.cli as cli
    monkeypatch.setattr(cli, "WINDOWS", True)
    monkeypatch.setattr(cli, "state_dir", lambda kitchen: tmp_path)
    monkeypatch.setattr(cli, "resolve_kitchen", lambda kitchen: "k")
    monkeypatch.setattr(cli, "resolve_project", lambda path: tmp_path)
    monkeypatch.setattr(cli, "spawn_window", lambda **kw: pytest.fail("launched a cook Windows can't run"))
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: pytest.fail(f"ran {a[0]}"))
    for backend, clean_room, slot in (("codex", False, None), ("claude", True, None), ("claude", False, "repo")):
        args = argparse.Namespace(kitchen="k", name="eng", backend=backend, clean_room=clean_room, slot=slot,
                                  project=None, role=None, effort=None, with_skill=[], swarm_account=None,
                                  model=None)
        with pytest.raises(SystemExit, match="On Windows"):
            cli.cmd_hire(args)
        assert not (tmp_path / "cooks").exists(), (backend, clean_room, slot)


def test_help_command_prints_the_commands(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["kitchen", "help"])
    main()
    assert "brigade" in capsys.readouterr().out
