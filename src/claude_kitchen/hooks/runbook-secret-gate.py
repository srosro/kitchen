#!/usr/bin/env python3
"""PreToolUse[Edit|Write|MultiEdit|Bash] gate: deny a write into the operator's
runbook wikis (PERSONAL_RUNBOOK, COMPANY_RUNBOOK in ~/.config/kitchen/operator.env)
that carries a credential shape. The wikis sync to other machines and teammates,
so a secret written there has already left.

Edit/Write/MultiEdit are gated on the resolved target path and the text being
written. A Bash command is gated whole when any word in it (split the way the
shell would, quotes kept together; `~` and `$VARS` expanded, relative to the
session's cwd, symlinks resolved) lands in a wiki, and what it scans is
everything the command can see without running: its text, that text with the
environment expanded (`printf "$TOKEN" > …`), and every file outside the wikis it
names (`cp ~/.env …`, `cat creds >> …`). That over-blocks a read like
`grep token= <wiki>`, which the agent can rephrase, rather than parse what a
shell command writes. Output of a command substitution, or a program that opens
the wiki itself, is out of a pre-run hook's reach. Fails open on a malformed
request or a machine without the wiki settings: this hook must never wedge
every tool call.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _roborev_hooklib import _split_into_segments
from _secret_patterns import contains_secret

ROOT_KEYS = ("PERSONAL_RUNBOOK", "COMPANY_RUNBOOK")
MAX_SCAN = 1 << 20  # bytes of one named file read for scanning
PATH_WORD = re.compile(r"""[~/$][^\s'"<>|;&()`]*""")


def _settings() -> dict[str, str]:
    env = Path.home() / ".config" / "kitchen" / "operator.env"
    try:
        lines = env.read_text().splitlines()
    except OSError:
        return {}
    out = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip() in ROOT_KEYS:
            out[key.strip()] = value.strip().strip("\"'")
    return out


def _resolve(word: str, variables: dict[str, str], cwd: str = "/") -> Path:
    for key, value in variables.items():
        word = word.replace("${%s}" % key, value).replace("$" + key, value)
    return (Path(cwd) / os.path.expandvars(os.path.expanduser(word))).resolve()


def _words(command: str) -> list[str]:
    """Shell words, or path-like runs when the command doesn't parse (a heredoc body's apostrophe)."""
    segments = _split_into_segments(command)
    return [w for segment in segments for w in segment] if segments else PATH_WORD.findall(command)


def _in_wiki(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace") if path.is_file() and path.stat().st_size <= MAX_SCAN else ""
    except OSError:
        return ""


def _written_text(tool: str, args: dict) -> str:
    if tool == "Write":
        return args.get("content") or ""
    if tool == "Edit":
        return args.get("new_string") or ""
    return "\n".join(e.get("new_string") or "" for e in args.get("edits") or [])


def main() -> int:
    try:
        request = json.load(sys.stdin)
        tool, args, cwd = request["tool_name"], request["tool_input"], request.get("cwd") or "/"
    except (ValueError, KeyError, TypeError):
        return 0
    variables = _settings()
    roots = [_resolve(v, {}) for v in variables.values() if v]
    if not roots:
        return 0
    if tool == "Bash":
        command = args.get("command") or ""
        paths = [_resolve(w, variables, cwd) for w in _words(command)]
        hit = any(_in_wiki(p, roots) for p in paths)
        text = "\n".join([command, os.path.expandvars(command),
                          *(_read(p) for p in paths if not _in_wiki(p, roots))])
    else:
        hit = _in_wiki(_resolve(args.get("file_path") or "/", variables), roots)
        text = _written_text(tool, args)
    if not (hit and contains_secret(text)):
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            "not written: this write to a runbook wiki carries a credential shape. Runbooks sync to other "
            "machines and teammates: name where the secret lives (the vault item), never its value; mask "
            "anything you must reference (…*** below 8 characters, otherwise the last 3)."),
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
