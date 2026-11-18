"""Runs inside HQ's synapse container: retire one room as `@plow`, history kept.

`hq_room.py` sends this file with a `main(<target>)` call appended. It logs in
from the task's own environment, refuses anything but one ordinary HQ room,
makes the one admin call with purge off, reads the room back, logs out, and
prints one `HQ_ROOM <json>` line. Neither the password nor the token is printed.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

SERVER = "plow.co"
BASE = "http://localhost:8008"
# Rooms swarm itself keeps: the Spaces, #general, the Admin log and its own room.
CORE_ALIASES = ("general", "admin-log", "swarm-module", "rooms", "guests")
q = lambda s: urllib.parse.quote(s, safe="")


def call(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"content-type": "application/json",
                                          **({"authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def resolve(alias, token):
    status, out = call("GET", "/_matrix/client/v3/directory/room/" + q(alias), token=token)
    return out["room_id"] if status == 200 else None


def core_rooms(token):
    """Which rooms swarm keeps, as the server lists them: the core aliases and
    the common rooms #general names in its `swarm.common_rooms`."""
    core = {resolve(f"#{a}:{SERVER}", token): f"#{a}" for a in CORE_ALIASES}
    general = next(r for r, a in core.items() if a == "#general")
    status, listed = call("GET", f"/_matrix/client/v3/rooms/{q(general)}/state/swarm.common_rooms/", token=token)
    for alias in listed.get("rooms", []) if status == 200 else []:
        core.setdefault(resolve(alias, token), f"{alias} (a common room)")
    core.pop(None, None)
    return core


def snapshot(room, token):
    status, d = call("GET", "/_synapse/admin/v1/rooms/" + q(room), token=token)
    if status != 200:
        return None
    return {k: d.get(k) for k in ("name", "canonical_alias", "joined_members", "public", "room_type", "creator")}


def retire(target, token):
    room = resolve(target, token) if target.startswith("#") else target
    before = room and snapshot(room, token)
    if not before:
        return {"refused": f"{target} is no room on HQ ({SERVER})"}
    out = {"room_id": room, "before": before}
    core = core_rooms(token)
    if room in core or before["room_type"] == "m.space":
        return {**out, "refused": f"{core.get(room, 'a Space')} is one of HQ's core rooms: it stays"}
    status, started = call("DELETE", "/_synapse/admin/v2/rooms/" + q(room),
                           {"purge": False, "block": False, "message": "This room was closed by Plow."}, token)
    if status != 200:
        return {**out, "error": started}
    for _ in range(60):
        _, result = call("GET", "/_synapse/admin/v2/rooms/delete_status/" + q(started["delete_id"]), token=token)
        if result.get("status") in ("complete", "failed"):
            break
        time.sleep(1)
    shut = result.get("shutdown_room") or {}
    out = {**out, "status": result.get("status"), "error": result.get("error"),
           "kicked": shut.get("kicked_users"), "failed_to_kick": shut.get("failed_to_kick_users")}
    if out["status"] == "complete":
        unlist(room, token)
    return {**out, "after": {**snapshot(room, token), **listings(room, token)}}


def listings(room, token):
    """Where the directory still shows `room`: its local addresses, and the
    Spaces that list it as a child."""
    _, aliases = call("GET", f"/_matrix/client/v3/rooms/{q(room)}/aliases", token=token)
    spaces = []
    for alias in ("#rooms:" + SERVER, "#guests:" + SERVER):
        status, child = call("GET", f"/_matrix/client/v3/rooms/{q(resolve(alias, token))}/state/m.space.child/{q(room)}", token=token)
        if status == 200 and child:
            spaces.append(alias)
    return {"aliases": aliases["aliases"], "in_spaces": spaces}


def unlist(room, token):
    """The shutdown keeps a room's addresses and its Space listing; take both
    away, as swarm's own room deletion does."""
    listed = listings(room, token)
    for alias in listed["aliases"]:
        call("DELETE", "/_matrix/client/v3/directory/room/" + q(alias), token=token)
    for alias in listed["in_spaces"]:
        call("PUT", f"/_matrix/client/v3/rooms/{q(resolve(alias, token))}/state/m.space.child/{q(room)}", {}, token)


def main(target):
    status, login = call("POST", "/_matrix/client/v3/login", {
        "type": "m.login.password", "identifier": {"type": "m.id.user", "user": "plow"},
        "password": os.environ["SWARM_PLOW_PASSWORD"], "initial_device_display_name": "ops kitchen hq-room"})
    if status != 200:
        print("HQ_ROOM " + json.dumps({"error": {"login": status, **{k: login.get(k) for k in ("errcode", "error")}}}))
        return
    token = login["access_token"]
    try:
        print("HQ_ROOM " + json.dumps(retire(target, token)))
    finally:
        call("POST", "/_matrix/client/v3/logout", {}, token)
