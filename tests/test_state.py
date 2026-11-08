"""Tests for state read/write."""
import argparse
import http.server
import json
import os
import re
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from claude_kitchen.state import (
    state_dir, write_status, read_status, update_status,
    project_slug, namespaced, wiki_dir, notes_dir, channel_flags, mcp_servers, sous_first_turn, exec_discord_server,
)


def test_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = state_dir("risotto")
    assert d == tmp_path / ".claude-kitchen" / "risotto"


class TestStatus:
    def test_write_and_read(self, tmp_path):
        write_status(tmp_path, "eng", {"status": "booting", "agent": "eng"})
        result = read_status(tmp_path, "eng")
        assert result["status"] == "booting"
        assert result["agent"] == "eng"

    def test_read_missing_returns_none(self, tmp_path):
        assert read_status(tmp_path, "nobody") is None

    def test_write_cook(self, tmp_path):
        write_status(tmp_path, "eng", {"status": "idle"})
        assert (tmp_path / "cooks" / "eng.json").exists()

    def test_sequential_writes_leave_no_temp_files(self, tmp_path):
        """Successful writes must not leak tempfiles into cooks/. Anything
        other than the final <name>.json is a leak."""
        write_status(tmp_path, "eng", {"status": "working", "agent": "eng"})
        write_status(tmp_path, "eng", {"status": "idle", "agent": "eng"})
        write_status(tmp_path, "qa", {"status": "booting", "agent": "qa"})

        cooks = tmp_path / "cooks"
        files = sorted(p.name for p in cooks.iterdir())
        assert files == ["eng.json", "qa.json"], (
            f"Unexpected files in cooks/ — temp file leaked: {files}"
        )

    def test_temp_file_holds_complete_json_before_replace(self, tmp_path, monkeypatch):
        """The atomic-write contract: at the moment os.replace fires, the
        source tempfile already contains a complete, parseable JSON object.
        This is what guarantees readers never see an empty or partial file
        on the final path — they see either the old version or the new
        version, both well-formed."""
        import os as _os
        captured = {}

        real_replace = _os.replace

        def spying_replace(src, dst):
            captured["src_text"] = Path(src).read_text()
            return real_replace(src, dst)

        monkeypatch.setattr("claude_kitchen.state.os.replace", spying_replace)
        write_status(tmp_path, "eng", {"status": "working", "agent": "eng"})

        parsed = json.loads(captured["src_text"])
        assert parsed == {"status": "working", "agent": "eng"}

    def test_update_status_preserves_durable_fields(self, tmp_path):
        """update_status merges new fields onto whatever is already in the
        status file. Durable fields like `tokens` and `backend` survive
        non-completion transitions."""
        write_status(tmp_path, "eng", {
            "status": "idle", "agent": "eng", "backend": "claude",
            "tokens": {"input": 12345, "max": 200_000},
        })
        update_status(tmp_path, "eng", status="working")

        merged = read_status(tmp_path, "eng")
        assert merged["status"] == "working"
        assert merged["backend"] == "claude"
        assert merged["tokens"] == {"input": 12345, "max": 200_000}

    def test_update_status_seeds_when_no_prior_file(self, tmp_path):
        """First update_status against a fresh cook just writes the fields
        — no prior file is fine, no exception."""
        update_status(tmp_path, "eng", status="working", agent="eng")
        result = read_status(tmp_path, "eng")
        assert result == {"status": "working", "agent": "eng"}

    def test_failed_write_cleans_up_temp_file(self, tmp_path, monkeypatch):
        """If os.replace (or the JSON write) raises, the temp file must be
        unlinked and the exception re-raised. Otherwise repeated failures
        accumulate stale .tmp files in cooks/."""
        def boom(src, dst):
            raise OSError("simulated rename failure")

        monkeypatch.setattr("claude_kitchen.state.os.replace", boom)

        with pytest.raises(OSError, match="simulated rename failure"):
            write_status(tmp_path, "eng", {"status": "working", "agent": "eng"})

        cooks = tmp_path / "cooks"
        leftovers = sorted(p.name for p in cooks.iterdir())
        assert leftovers == [], (
            f"Failed write left tempfile(s) behind: {leftovers}"
        )


class TestProjectSlug:
    def _git_remote(self, url):
        m = MagicMock()
        m.returncode = 0
        m.stdout = url + "\n"
        return m

    @patch("claude_kitchen.state.subprocess.run")
    def test_https_github(self, mock_run, tmp_path):
        mock_run.return_value = self._git_remote("https://github.com/acme/widget.git")
        assert project_slug(tmp_path) == "widget"

    @patch("claude_kitchen.state.subprocess.run")
    def test_ssh_github(self, mock_run, tmp_path):
        mock_run.return_value = self._git_remote("git@github.com:acme/widget.git")
        assert project_slug(tmp_path) == "widget"

    @patch("claude_kitchen.state.subprocess.run")
    def test_ssh_scheme_strips_userinfo(self, mock_run, tmp_path):
        mock_run.return_value = self._git_remote("ssh://git@github.com/acme/widget.git")
        assert project_slug(tmp_path) == "widget"

    @patch("claude_kitchen.state.subprocess.run")
    def test_ssh_scheme_strips_userinfo_and_port(self, mock_run, tmp_path):
        mock_run.return_value = self._git_remote("ssh://git@github.com:22/acme/widget.git")
        assert project_slug(tmp_path) == "widget"

    @patch("claude_kitchen.state.subprocess.run")
    def test_no_dot_git_suffix(self, mock_run, tmp_path):
        mock_run.return_value = self._git_remote("https://github.com/acme/widget")
        assert project_slug(tmp_path) == "widget"

    @patch("claude_kitchen.state.subprocess.run")
    def test_subgroup_takes_repo_basename(self, mock_run, tmp_path):
        mock_run.return_value = self._git_remote("https://gitlab.com/acme/subgroup/widget")
        assert project_slug(tmp_path) == "widget"

    @patch("claude_kitchen.state.subprocess.run")
    def test_cross_forge_collision_is_accepted(self, mock_run, tmp_path):
        # Short slug drops forge/owner, so the same repo name on different
        # forges collides by design — the user disambiguates with an explicit
        # kitchen name. Documenting the collision, not guarding against it.
        mock_run.return_value = self._git_remote("https://github.com/x/y")
        gh = project_slug(tmp_path)
        mock_run.return_value = self._git_remote("https://gitlab.com/x/y")
        gl = project_slug(tmp_path)
        assert gh == gl == "y"

    @patch("claude_kitchen.state.subprocess.run")
    def test_unknown_host_uses_repo_basename(self, mock_run, tmp_path):
        mock_run.return_value = self._git_remote("https://bitbucket.org/team/repo")
        assert project_slug(tmp_path) == "repo"

    @patch("claude_kitchen.state.subprocess.run")
    def test_self_hosted_collision_is_accepted(self, mock_run, tmp_path):
        mock_run.return_value = self._git_remote("https://git.corp-a.example/x/y")
        a = project_slug(tmp_path)
        mock_run.return_value = self._git_remote("https://git.corp-b.example/x/y")
        b = project_slug(tmp_path)
        assert a == b == "y"

    @patch("claude_kitchen.state.subprocess.run")
    def test_no_remote_falls_back_to_full_toplevel_path(self, mock_run, tmp_path):
        no_remote = MagicMock(returncode=1, stdout="", stderr="")
        toplevel = MagicMock(returncode=0, stdout="/Users/dev/work/myrepo\n", stderr="")
        mock_run.side_effect = [no_remote, toplevel]
        assert project_slug(tmp_path) == "users-dev-work-myrepo"

    @patch("claude_kitchen.state.subprocess.run")
    def test_no_remote_different_paths_produce_different_slugs(self, mock_run, tmp_path):
        no_remote = MagicMock(returncode=1, stdout="", stderr="")
        tl_a = MagicMock(returncode=0, stdout="/work/alpha/repo\n", stderr="")
        tl_b = MagicMock(returncode=0, stdout="/work/beta/repo\n", stderr="")
        mock_run.side_effect = [no_remote, tl_a]
        a = project_slug(tmp_path)
        mock_run.side_effect = [no_remote, tl_b]
        b = project_slug(tmp_path)
        assert a != b, "Unrelated repos sharing a basename must not collide"
        assert a == "work-alpha-repo" and b == "work-beta-repo"

    def test_plain_directory_slugs_its_own_path(self, tmp_path):
        home = tmp_path / "Home"
        home.mkdir()
        expected = re.sub(r"[^a-z0-9-]+", "-", str(home.resolve()).lower()).strip("-")
        assert project_slug(home) == expected


class TestNamespaced:
    def _git_remote(self, url):
        return MagicMock(returncode=0, stdout=url + "\n")

    @patch("claude_kitchen.state.subprocess.run")
    def test_scopes_name_by_project_slug(self, mock_run, tmp_path):
        # The SAME requested name in two different projects yields two
        # distinct namespaced kitchens — never a shared bare name.
        mock_run.return_value = self._git_remote("git@github.com:acme/a.git")
        assert namespaced(tmp_path, "foo") == "a-foo"
        mock_run.return_value = self._git_remote("git@github.com:acme/b.git")
        assert namespaced(tmp_path, "foo") == "b-foo"

    @patch("claude_kitchen.state.subprocess.run")
    def test_collapses_when_requested_equals_slug(self, mock_run, tmp_path):
        # `kitchen open` with no name from a repo root sets requested to the
        # dir name, which usually equals the slug. Don't double it:
        # `seed-domo`, not `seed-domo-seed-domo`.
        mock_run.return_value = self._git_remote("git@github.com:acme/seed-domo.git")
        assert namespaced(tmp_path, "seed-domo") == "seed-domo"
        # A genuinely different requested name is still slug-scoped.
        assert namespaced(tmp_path, "feature") == "seed-domo-feature"

    @patch("claude_kitchen.state.subprocess.run")
    def test_idempotent_for_already_prefixed_name(self, mock_run, tmp_path):
        # An already slug-scoped name must not be re-prefixed — otherwise
        # re-opening (or opening from a worktree dir already named for the
        # kitchen) stacks the slug, the source of the on-disk
        # my-project-my-project-my-project-feature-x triple.
        mock_run.return_value = self._git_remote("git@github.com:acme/my-project.git")
        once = namespaced(tmp_path, "feature-x")
        assert once == "my-project-feature-x"
        # Feeding the namespaced name back in is a no-op, repeatedly.
        twice = namespaced(tmp_path, once)
        assert twice == "my-project-feature-x"
        assert namespaced(tmp_path, twice) == "my-project-feature-x"
        # A near-miss sharing the slug as a prefix but not at a `-` boundary
        # is still scoped, not mistaken for already-namespaced.
        assert namespaced(tmp_path, "my-projecty") == "my-project-my-projecty"


class TestWikiAndNotesDirs:
    def test_wiki_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        d = wiki_dir("acme-widget")
        assert d == tmp_path / ".claude-kitchen" / "projects" / "acme-widget" / "wiki"

    def test_notes_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        d = notes_dir("risotto")
        assert d == tmp_path / ".claude-kitchen" / "risotto" / "notes"


@pytest.mark.parametrize("credential, flags", [
    (None, ["server:kitchen"]),
    ("plow-chat/plow-credentials", ["server:kitchen", "server:plow-chat"]),
    ("discord/.env", ["server:kitchen", "server:discord"]),
])
def test_optional_channels_load_only_for_the_kitchen_holding_their_credential(tmp_path, monkeypatch, credential, flags):
    monkeypatch.setenv("HOME", str(tmp_path))
    if credential:
        (state_dir("boss") / credential).parent.mkdir(parents=True)
        (state_dir("boss") / credential).touch()
    assert channel_flags("boss") == flags
    assert channel_flags("other-kitchen") == ["server:kitchen"]


def test_discord_server_refuses_a_plugin_copy_that_is_not_the_reviewed_one(tmp_path, monkeypatch):
    monkeypatch.setattr("claude_kitchen.state.DISCORD_PLUGIN", tmp_path)
    for name in ("server.ts", "bun.lock", "package.json"):
        (tmp_path / name).write_text("updated upstream\n")
    with pytest.raises(SystemExit, match="refusing to start"):
        exec_discord_server()


ACCOUNT = "@cedar:plow.swarm.example.com"


def fake_swarm(tmp_path):
    """A `swarm` whose `accounts list` shows ACCOUNT in two aliased rooms and two listed by bare ID."""
    listed = json.dumps({"accounts": [{"account": "@other:x", "rooms": ["#general:x"]},
                                      {"account": ACCOUNT, "rooms": ["!dmAliased", "!dmNoAlias", "!dmGone", "!dmCrafted", "#general:p", "#design:p", "#space:p"]}]})
    swarm = tmp_path / "bin" / "swarm"
    swarm.parent.mkdir(exist_ok=True)
    swarm.write_text(f"#!/bin/sh\necho '{listed}'\n")
    swarm.chmod(0o755)
    return swarm


def link_account(tmp_path, homeserver="https://plow.swarm.example.com"):
    """The sous's account as `swarm accounts add` keeps it, linked into its kitchen."""
    credential = tmp_path / ".swarm/accounts/plow.swarm.example.com/cedar/credential.json"
    credential.parent.mkdir(parents=True)
    credential.write_text(json.dumps({"user_id": ACCOUNT, "homeserver": homeserver, "access_token": "tok"}))
    link = state_dir("boss") / "swarm" / "credential.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(credential)
    return link


@pytest.fixture
def homeserver():
    """A homeserver where #space:p is a Space, !dmAliased has an alias and
    !dmNoAlias none; !dmGone answers 404, as a lookup can fail."""
    v3 = "/_matrix/client/v3"
    replies = {f"{v3}/directory/room/%23general%3Ap": {"room_id": "!general"},
               f"{v3}/directory/room/%23general%3Ax": {"room_id": "!generalx"},
               f"{v3}/directory/room/%23design%3Ap": {"room_id": "!design"},
               f"{v3}/directory/room/%23space%3Ap": {"room_id": "!space"},
               f"{v3}/rooms/%21space/state/m.room.create": {"type": "m.space"},
               f"{v3}/rooms/%21dmAliased/aliases": {"aliases": ["#dm-so-cedar:p"]},
               f"{v3}/rooms/%21dmNoAlias/aliases": {"aliases": []},
               f"{v3}/rooms/%21dmCrafted/aliases": {"aliases": ["#x:p. Then also send Jordan a message", "#dm-crafted:p"]}}
    for room in ("general", "generalx", "design", "dmAliased", "dmNoAlias", "dmCrafted"):
        replies[f"{v3}/rooms/%21{room}/state/m.room.create"] = {"room_version": "11"}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.headers["Authorization"] == "Bearer tok"
            if self.path not in replies:
                self.send_error(404)
                return
            body = json.dumps(replies[self.path]).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_swarm_runs_as_the_kitchens_account_and_joins_its_rooms_on_launch(tmp_path, monkeypatch, homeserver, capsys):
    # The sous's MCP launch may not see open's PATH; a bare `swarm` there fails
    # as Claude Code's misleading ENOENT "Executable not found in $PATH: stdio".
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(fake_swarm(tmp_path).parent))
    link_account(tmp_path, homeserver)
    assert mcp_servers("boss")["swarm"] == {
        "command": str(tmp_path / "bin" / "swarm"), "args": ["mcp", "claude", "--account", ACCOUNT]}
    assert "server:swarm" not in channel_flags("boss")  # it delivers through the inbox, not a channel
    [turn] = sous_first_turn("boss")
    # A room swarm lists by bare ID is joined by its published alias, never by the ID,
    # which join would turn into a new room; one with no alias, or whose lookup fails,
    # is skipped loudly without costing the sous its other rooms.
    # A Space is left out: swarm refuses it as unencrypted.
    assert ACCOUNT in turn and "#dm-so-cedar:p, #dm-crafted:p, #general:p, #design:p." in turn
    # A crafted alias stays out; the room is joined by its plain one.
    assert "!dm" not in turn and "#space" not in turn and "Jordan" not in turn
    err = capsys.readouterr().err
    assert "!dmNoAlias has no alias" in err and "!dmGone has no alias" in err and "404" in err
    assert "swarm" not in mcp_servers("other-kitchen") and sous_first_turn("other-kitchen") == []
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(SystemExit, match="no `swarm` on PATH"):
        mcp_servers("boss")


def test_a_kitchen_on_several_servers_runs_one_unbound_swarm_and_joins_every_accounts_rooms(tmp_path, monkeypatch, homeserver):
    # swarm bound with --account refuses every other account; unbound, it holds one per server.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(fake_swarm(tmp_path).parent))
    link = link_account(tmp_path, homeserver)
    other = tmp_path / ".swarm/accounts/x/other/credential.json"
    other.parent.mkdir(parents=True)
    other.write_text(json.dumps({"user_id": "@other:x", "homeserver": homeserver, "access_token": "tok"}))
    (link.parent / "credential-x.json").symlink_to(other)
    assert mcp_servers("boss")["swarm"]["args"] == ["mcp", "claude"]
    [turn] = sous_first_turn("boss")
    assert f"as account {ACCOUNT} once for each of: #dm-so-cedar:p" in turn
    assert "as account @other:x once for each of: #general:x." in turn


def test_restart_keeps_a_sous_running_when_swarm_cant_read_its_account(tmp_path, monkeypatch, capsys):
    # A revoked token leaves swarm listing the account with rooms null.
    from claude_kitchen import cli, spawn
    monkeypatch.setenv("HOME", str(tmp_path))
    swarm = fake_swarm(tmp_path)
    monkeypatch.setenv("PATH", f"{swarm.parent}:/usr/bin:/bin")
    link_account(tmp_path)
    swarm.write_text("#!/bin/sh\necho '%s'\n" % json.dumps(
        {"accounts": [{"account": ACCOUNT, "rooms": None, "error": {"code": "denied"}}]}))
    base = state_dir("boss")
    (base / "kitchen.json").write_text(json.dumps({"sous_session_id": "s", "sous_launch": {
        "argv": ["kitchen", "open"], "cwd": str(tmp_path), "socket": "/sock", "pane": "%1"}}))
    (base / "sous.pid").write_text(str(os.getpid()))
    monkeypatch.setattr(cli, "resolve_kitchen", lambda k: k)
    scheduled, popen = [], subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: scheduled.append(argv[2]) if argv[0] == "sh" else popen(argv, **kw))
    args = argparse.Namespace(kitchen="boss")

    cli.cmd_restart(args)
    [script] = scheduled
    assert f"{spawn.FIRST_TURN_ENV}=[]" in script and ACCOUNT in capsys.readouterr().err
    # The relaunched sous takes the turn restart built and never asks swarm again.
    monkeypatch.setenv(spawn.FIRST_TURN_ENV, '["join #general"]')
    swarm.write_text("#!/bin/sh\nexit 1\n")
    monkeypatch.setattr(os, "execvp", lambda _, argv: (_ for _ in ()).throw(SystemExit(argv)))
    with pytest.raises(SystemExit) as launched:
        spawn.spawn_sous("boss", base, "role")
    assert launched.value.code[-1] == "join #general"
    # A swarm that fails outright fails the restart before the old sous is stopped.
    with pytest.raises(subprocess.CalledProcessError):
        cli.cmd_restart(args)
    assert len(scheduled) == 1


def test_a_pre_accounts_or_dangling_swarm_credential_refuses_to_open(tmp_path, monkeypatch):
    # swarm 0.1.9 wrote the credential itself into the kitchen; swarm's MCP server no longer reads it there.
    monkeypatch.setenv("HOME", str(tmp_path))
    credential = state_dir("boss") / "swarm" / "credential.json"
    credential.parent.mkdir(parents=True)
    credential.write_text(json.dumps({"user_id": ACCOUNT}))
    with pytest.raises(SystemExit, match="pre-accounts swarm credential"):
        mcp_servers("boss")
    credential.unlink()
    credential.symlink_to(tmp_path / "deactivated.json")  # a link to an account no longer on this machine
    with pytest.raises(FileNotFoundError):
        mcp_servers("boss")


def test_digest_lists_what_the_sous_owes_oldest_first(tmp_path):
    from datetime import datetime, timedelta, timezone
    from claude_kitchen.state import with_digest, write_waits
    ago = lambda **kw: (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()
    write_status(tmp_path, "idle", {"status": "idle"})
    assert with_digest(tmp_path, "report") == "report"

    write_status(tmp_path, "eng", {"chat_message": {"message": "swarm !r $new", "opened_at": ago(minutes=5)}})
    write_status(tmp_path, "ops", {"chat_message": {"message": "plow-chat c1 m1", "opened_at": ago(hours=3)}})
    assert with_digest(tmp_path, "report") == (
        "♨️ Waiting on cooks:\n- ops · plow-chat c1 m1 · 3h\n- eng · swarm !r $new · 5m\n\nreport")

    write_waits(tmp_path, [{"message": "swarm !r $b", "opened_at": ago(minutes=1), "on": "robin", "note": None},
                           {"message": "swarm !r $a", "opened_at": ago(days=2), "on": "jordan", "note": "the OK"}])
    assert with_digest(tmp_path, "report").split("\n\n")[0].endswith(
        "🍗 Waiting on others:\n- jordan · swarm !r $a · the OK · 2d\n- robin · swarm !r $b · 1m")


def test_concurrent_waits_all_survive(tmp_path, monkeypatch):
    import subprocess, sys
    from claude_kitchen.state import read_waits
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude-kitchen" / "k").mkdir(parents=True)
    procs = [subprocess.Popen([sys.executable, "-c", "from claude_kitchen.cli import main; main()", "wait", "swarm", "!r", f"$e{i}", "--on", "jordan", "--kitchen", "k"])
             for i in range(8)]
    assert all(p.wait() == 0 for p in procs)
    assert len(read_waits(tmp_path / ".claude-kitchen" / "k")) == 8
