#!/usr/bin/env python3
"""PreToolUse[Edit|Write|MultiEdit|Bash] gate: deny a write into the operator's
runbook wikis (PERSONAL_RUNBOOK, COMPANY_RUNBOOK in ~/.config/kitchen/operator.env)
that carries a credential shape. The wikis sync to other machines and teammates,
so a secret written there has already left.

Edit/Write/MultiEdit are gated on the resolved target path and the text being
written. A Bash command is gated whole when any path-like word in it (with `~`
and `$VARS` expanded, symlinks resolved) lands in a wiki: that over-blocks a read
like `grep token= <wiki>`, which the agent can rephrase, rather than parse what a
shell command writes. Fails open on a malformed request or a machine without the
wiki settings: this hook must never wedge every tool call.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _secret_patterns import contains_secret

ROOT_KEYS = ("PERSONAL_RUNBOOK", "COMPANY_RUNBOOK")
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


def _resolve(word: str, variables: dict[str, str]) -> Path:
    for key, value in variables.items():
        word = word.replace("${%s}" % key, value).replace("$" + key, value)
    return Path(os.path.expandvars(os.path.expanduser(word))).resolve()


def _in_wiki(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _written_text(tool: str, args: dict) -> str:
    if tool == "Write":
        return args.get("content") or ""
    if tool == "Edit":
        return args.get("new_string") or ""
    return "\n".join(e.get("new_string") or "" for e in args.get("edits") or [])


def main() -> int:
    try:
        request = json.load(sys.stdin)
        tool, args = request["tool_name"], request["tool_input"]
    except (ValueError, KeyError, TypeError):
        return 0
    variables = _settings()
    roots = [_resolve(v, {}) for v in variables.values() if v]
    if not roots:
        return 0
    if tool == "Bash":
        command = args.get("command") or ""
        hit = any(_in_wiki(_resolve(w, variables), roots) for w in PATH_WORD.findall(command))
        text = command
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
