"""Retiring an HQ room: the task script against a fake Synapse, and the local target check."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import pytest

from claude_kitchen import hq_room, hq_room_task

SAM, AGENT, SERVICE = "@sam:plow.co", "@cedar:plow.co", "@plow:plow.co"
RULES = {"approver": SAM, "agents": [AGENT], "keep": ["#so-cos:plow.co", "!household"]}
# room: (creator, name, room_type)
ROOMS = {"!general": (SERVICE, "general", None), "!log": (SERVICE, "Admin log", None), "!module": (SERVICE, "Swarm module", None),
         "!feedback": (AGENT, "Plow's Feedback", None), "!space": (SERVICE, "All Plow HQ", "m.space"),
         "!guests": (SERVICE, "Plow HQ guests", "m.space"), "!team-space": (AGENT, "team", "m.space"),
         "!work": (AGENT, "work", None), "!other": (AGENT, "other", None), "!cos": (AGENT, "so-cos", None),
         "!household": (SAM, "household", None), "!customer": ("@mary:plow.co", "mary-ea", None),
         "!family": (None, "Plow ❤️ Odio-family", None), "!dm": (AGENT, None, None)}
ALIASES = {"#general:plow.co": "!general", "#admin-log:plow.co": "!log", "#swarm-module:plow.co": "!module", "#plow-feedback:plow.co": "!feedback", "#rooms:plow.co": "!space", "#guests:plow.co": "!guests",
           "#work:plow.co": "!work", "#so-cos:plow.co": "!cos"}
NOW = int(time.time() * 1000)
HOUR = 3600 * 1000


def prompt(room="!work", sender=AGENT, age=HOUR):
    return {"type": "m.room.encrypted", "sender": sender, "room_id": room, "origin_server_ts": NOW - age, "content": {}}


def reaction(on="$prompt", key="🗑️", sender=SAM, room="!work", age=HOUR // 2, kind="m.reaction"):
    return {"type": kind, "sender": sender, "room_id": room, "origin_server_ts": NOW - age,
            "content": {"m.relates_to": {"rel_type": "m.annotation", "event_id": on, "key": key}}}


class Requests(list):
    """Each request the fake saw; `broken`: (method, path part) pairs it answers with a 500;
    `joined`: rooms' joined member counts, 0 when unset; `events`: what fetch_event serves."""


@pytest.fixture
def synapse(monkeypatch):
    """A fake Synapse: records every request, serves the reads the task makes."""
    seen, aliases, children, broken, joined = Requests(), dict(ALIASES), {"!work": {"via": ["plow.co"]}}, set(), {}
    events = {"$prompt": prompt(), "$ok": reaction()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def handle_any(self):
            path = unquote(self.path)
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"null")
            seen.append((self.command, path, body))
            if any(m == self.command and b in path for m, b in broken):
                return self.reply(500, {"errcode": "M_UNKNOWN"})
            route = {
                ("POST", "/_matrix/client/v3/login"): lambda: (200, {"access_token": "syt_fake9f2"}),
                ("POST", "/_matrix/client/v3/logout"): lambda: (200, {}),
            }.get((self.command, path))
            if route:
                return self.reply(*route())
            if path.startswith("/_matrix/client/v3/directory/room/"):
                alias = path.rsplit("/", 1)[1]
                if self.command == "DELETE":
                    aliases.pop(alias)
                    return self.reply(200, {})
                return self.reply(200, {"room_id": aliases[alias]}) if alias in aliases else self.reply(404, {})
            if path == "/_matrix/client/v3/rooms/!general/state/swarm.common_rooms/":
                return self.reply(200, {"rooms": ["#general:plow.co", "#plow-feedback:plow.co"]})
            if path.startswith("/_synapse/admin/v1/fetch_event/"):
                found = events.get(path.rsplit("/", 1)[1])
                return self.reply(200, {"event": found}) if found else self.reply(404, {})
            if path.startswith("/_synapse/admin/v1/rooms/"):
                room = path.rsplit("/", 1)[1]
                if room not in ROOMS:
                    return self.reply(404, {})
                creator, name, kind = ROOMS[room]
                return self.reply(200, {"name": name, "creator": creator, "room_type": kind, "joined_members": joined.get(room, 0)})
            if self.command == "DELETE" and path.startswith("/_synapse/admin/v2/rooms/"):
                return self.reply(200, {"delete_id": "d1"})
            if path == "/_synapse/admin/v2/rooms/delete_status/d1":
                return self.reply(200, {"status": "complete", "shutdown_room": {"kicked_users": ["@a:plow.co"]}})
            if path.endswith("/aliases"):
                room = path.split("/")[-2]
                return self.reply(200, {"aliases": [a for a, r in aliases.items() if r == room]})
            if "/state/m.space.child/" in path:
                room = path.rsplit("/", 1)[1]
                if self.command == "PUT":
                    children[room] = body
                    return self.reply(200, {})
                return self.reply(200, children[room]) if room in children else self.reply(404, {})
            self.reply(404, {})

        do_GET = do_POST = do_PUT = do_DELETE = handle_any

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(hq_room_task, "BASE", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("SWARM_PLOW_PASSWORD", "pw-fake7c1")
    seen.broken, seen.joined, seen.events = broken, joined, events
    yield seen
    server.shutdown()


def run(target, capsys, asked_in="$ok"):
    hq_room_task.main(target, asked_in, RULES)
    out = capsys.readouterr().out
    assert "syt_fake9f2" not in out and "pw-fake7c1" not in out
    return hq_room.parse(out)


def test_a_room_is_retired_with_purge_off_and_taken_out_of_the_directory(synapse, capsys):
    result = run("#work:plow.co", capsys)
    deletes = [b for m, p, b in synapse if m == "DELETE" and p == "/_synapse/admin/v2/rooms/!work"]
    assert deletes == [{"purge": False, "block": False, "message": "This room was closed by Plow."}]
    assert result["status"] == "complete"
    assert result["after"]["aliases"] == [] and result["after"]["in_spaces"] == []
    assert synapse[-1][:2] == ("POST", "/_matrix/client/v3/logout")


@pytest.mark.parametrize("target, refusal", [
    ("#general:plow.co", "made by @plow:plow.co"),
    ("!feedback", "a common room"),
    ("!team-space", "a Space"),
    ("#so-cos:plow.co", "so-cos is on the operator's keep list"),
    ("!household", "keep list"),
    ("!customer", "made by @mary:plow.co"),
    ("!family", "made by another server"),
    ("!dm", "a direct chat"),
    ("!elsewhere", "no room on HQ"),
    ("#gone:plow.co", "no room on HQ"),
])
def test_a_kept_or_unknown_room_is_refused_before_any_change(synapse, capsys, target, refusal):
    assert refusal in run(target, capsys)["refused"]
    assert not [m for m, p, _ in synapse if m in ("DELETE", "PUT")]
    assert synapse[-1][:2] == ("POST", "/_matrix/client/v3/logout")


@pytest.mark.parametrize("target", ["#work:plow.swarm.plow.co", "!abc:odio.family", "work", "#a:plow.co.evil", "!a b"])
def test_a_target_off_hq_never_reaches_aws(monkeypatch, target):
    monkeypatch.setattr(hq_room, "run_in_task", lambda _: pytest.fail("reached AWS"))
    with pytest.raises(SystemExit, match="not an HQ room"):
        hq_room.retire(target, "$ok")


@pytest.mark.parametrize("broken, changed", [(("GET", "swarm.common_rooms"), False), (("DELETE", "/directory/room/#work"), True),
                                            (("POST", "/logout"), True)])
def test_a_failed_lookup_or_step_is_reported_as_an_error_and_still_logs_out(synapse, capsys, broken, changed):
    synapse.broken.add(broken)
    result = run("#work:plow.co", capsys)
    assert "500" in f"{result.get('error')} {result.get('logout_error')}"
    assert bool([m for m, p, _ in synapse if m == "DELETE" and "admin" in p]) == changed
    assert synapse[-1][:2] == ("POST", "/_matrix/client/v3/logout")


def test_every_attempt_is_logged_privately_even_with_no_result(monkeypatch, tmp_path):
    monkeypatch.setattr(hq_room, "state_root", lambda: tmp_path)
    monkeypatch.setattr(hq_room, "rules", lambda: RULES)
    monkeypatch.setattr(hq_room, "run_in_task", lambda _: "Cannot perform start session: EOF")
    with pytest.raises(SystemExit, match="printed no result"):
        hq_room.retire("#work:plow.co", "$ok")
    entry = json.loads((tmp_path / "hq-room.log").read_text())
    assert (entry["action"], entry["asked_in"], entry["target"], entry["result"]) == (
        "retire (irreversible)", "$ok", "#work:plow.co", {"error": "no result from the task"})
    assert (tmp_path / "hq-room.log").stat().st_mode & 0o777 == 0o600


def test_a_member_left_in_the_room_is_an_error(synapse, capsys):
    synapse.joined["!work"] = 1
    assert run("#work:plow.co", capsys)["error"] == "members are still in the room"


@pytest.mark.parametrize("events, refusal", [
    ({"$ok": reaction(key="✅")}, "not a 🗑️ reaction"),
    ({"$ok": reaction(kind="m.room.encrypted")}, "not a 🗑️ reaction"),
    ({"$ok": reaction(sender=AGENT)}, "@cedar:plow.co's, not @sam:plow.co's"),
    ({"$ok": reaction(room="!other")}, "in another room"),
    ({"$ok": reaction(age=25 * HOUR)}, "the 🗑️ is over a day old"),
    ({"$prompt": prompt(sender="@mary:plow.co")}, "not on a prompt one of the approver's agents sent"),
    ({"$prompt": prompt(room="!other")}, "not on a prompt one of the approver's agents sent"),
    ({"$ok": reaction(on="$gone")}, "not on a prompt one of the approver's agents sent"),
    ({"$prompt": prompt(age=25 * HOUR)}, "the prompt is over a day old"),
    ({"$ok": None}, "no event on HQ"),
])
def test_only_the_approvers_fresh_trash_on_an_agents_prompt_in_that_room_confirms(synapse, capsys, events, refusal):
    synapse.events.update(events)
    if synapse.events["$ok"] is None:
        del synapse.events["$ok"]
    assert refusal in run("#work:plow.co", capsys)["refused"]
    assert not [m for m, p, _ in synapse if m in ("DELETE", "PUT")]
    assert synapse[-1][:2] == ("POST", "/_matrix/client/v3/logout")


def test_the_operators_settings_are_read_and_an_unset_one_refuses(monkeypatch, tmp_path):
    env = tmp_path / "operator.env"
    env.write_text('HQ_ROOM_APPROVER=@sam:plow.co\nHQ_ROOM_AGENTS="@cedar:plow.co @birch:plow.co"\n')
    monkeypatch.setattr(hq_room, "OPERATOR_ENV", env)
    with pytest.raises(SystemExit, match="sets no HQ_ROOM_KEEP"):
        hq_room.rules()
    env.write_text(env.read_text() + "HQ_ROOM_KEEP=#so-cos:plow.co\n")
    assert hq_room.rules() == {"approver": "@sam:plow.co", "agents": ["@cedar:plow.co", "@birch:plow.co"], "keep": ["#so-cos:plow.co"]}
