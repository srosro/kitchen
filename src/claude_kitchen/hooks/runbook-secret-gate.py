#!/usr/bin/env python3
"""PreToolUse[Edit|Write|MultiEdit|Bash] gate: deny a write into the operator's
runbook wikis (PERSONAL_RUNBOOK, COMPANY_RUNBOOK in ~/.config/kitchen/operator.env)
that carries a credential shape. The wikis sync to other machines and teammates,
so a secret written there has already left. It also refuses any write to the
retired shared memory files, `preferences.md` and `mistakes.md` in `$KITCHEN_WIKI`
or a wiki (archived copies under `_archived/` excepted): learnings go to runbooks.

Edit/Write/MultiEdit are gated on the resolved target path and the text being
written. A Bash command is gated whole when it mentions a wiki without any
shell parsing: its text contains a spelling of a wiki root (as configured,
expanded, resolved, `$PERSONAL_RUNBOOK`, or relative to the session's cwd), the
cwd is inside a wiki, or a word in it resolves into one (a symlinked alias).
Words come from a crude split plus, when the command parses, its quoted words
(`"my notes.txt"`). What it then scans is
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
RETIRED = {"preferences.md", "mistakes.md"}
MAX_SCAN = 1 << 20  # bytes of one named file read for scanning
WORD_BREAK = re.compile(r"""[\s<>|;&()'"`]+""")


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


def _spellings(key: str, value: str, root: Path, cwd: Path) -> set[str]:
    names = {value, os.path.expanduser(value), str(root), "$" + key, "${%s}" % key}
    if cwd in root.parents:
        names.add(str(root.relative_to(cwd)))
    return names


def _in_wiki(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _retired(path: Path, roots: list[Path]) -> bool:
    kitchen_wiki = os.environ.get("KITCHEN_WIKI")
    in_scope = _in_wiki(path, roots) or (kitchen_wiki and path.parent == Path(kitchen_wiki).resolve())
    return path.name in RETIRED and "_archived" not in path.parts and bool(in_scope)


def _deny(reason: str) -> int:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))
    return 0


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
    named = {k: (v, _resolve(v, {})) for k, v in variables.items() if v}
    roots = [root for _, root in named.values()]
    if not roots:
        return 0
    if tool == "Bash":
        command, here = args.get("command") or "", Path(cwd).resolve()
        words = set(WORD_BREAK.split(command)) | {w for seg in _split_into_segments(command) for w in seg}
        paths = [_resolve(w, variables, cwd) for w in words if w]
        hit = (_in_wiki(here, roots) or any(_in_wiki(p, roots) for p in paths)
               or any(s in command for k, (v, root) in named.items() for s in _spellings(k, v, root, here)))
        text = "\n".join([command, os.path.expandvars(command),
                          *(_read(p) for p in paths if not _in_wiki(p, roots))])
    else:
        paths = [_resolve(args.get("file_path") or "/", variables)]
        hit = _in_wiki(paths[0], roots)
        text = _written_text(tool, args)
    if any(_retired(p, roots) for p in paths):
        return _deny("refused: preferences.md and mistakes.md are retired. Write the learning in the runbook "
                     "its audience reads: anything about one person in your runbook, $PERSONAL_RUNBOOK/runbooks/"
                     "<your agent's folder name>.md; a practice every teammate's agent follows in a $COMPANY_RUNBOOK page.")
    if hit and contains_secret(text):
        return _deny("not written: this write to a runbook wiki carries a credential shape. Runbooks sync to other "
                     "machines and teammates: name where the secret lives (the vault item), never its value; mask "
                     "anything you must reference (…*** below 8 characters, otherwise the last 3).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
