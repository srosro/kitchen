#!/usr/bin/env python3
"""PreToolUse[Edit|Write|MultiEdit|Bash] gate: deny a write into the operator's
runbook wikis (PERSONAL_RUNBOOK, COMPANY_RUNBOOK in ~/.config/kitchen/operator.env)
that carries a credential shape. The wikis sync to other machines and teammates,
so a secret written there has already left. It also refuses the retired shared
memory files, `preferences.md` and `mistakes.md` in `$KITCHEN_WIKI` or a wiki
(archived copies under `_archived/` excepted): an Edit/Write to them, and any Bash
command naming them, reads included, since telling a Bash read from a write is
the shell parsing this gate avoids. Learnings go to runbooks; the Read tool still
reads.

Edit/Write/MultiEdit are gated on the resolved target path and the text being
written. A Bash command is judged one statement at a time (split on newlines
and unquoted `&&`, `||`, `;`, `&`; a heredoc body stays with the line that opens
it, and a pipeline stays whole), so a `kitchen ticket` beside a runbook append
isn't judged by the append's rules. A statement is judged when it mentions a
wiki without any shell parsing: its text contains a spelling of a wiki root (as
configured, expanded, resolved, `$PERSONAL_RUNBOOK`, or relative to the
session's cwd), the cwd is inside a wiki, or a word in it resolves into one (a
symlinked alias). Words come from a crude split plus, when the statement parses,
its quoted words (`"my notes.txt"`). What it then scans is everything that
statement can see without running: its text, that text with the environment
expanded (`printf "$TOKEN" > …`), and every file outside the wikis it names
(`cp ~/.env …`, `cat creds >> …`). That over-blocks a read like
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
from _roborev_hooklib import _deny, _split_into_segments
from _secret_patterns import contains_secret

ROOT_KEYS = ("PERSONAL_RUNBOOK", "COMPANY_RUNBOOK")
RETIRED = {"preferences.md", "mistakes.md"}
MAX_SCAN = 1 << 20  # bytes of one named file read for scanning
HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)")
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


def _split_unquoted(line: str) -> list[str]:
    """`line` cut at `&&`, `||`, `;` and a lone `&` outside quotes; `|` stays, so a pipeline is one piece."""
    parts, start, quote, i = [], 0, None, 0
    while i < len(line):
        c = line[i]
        if c == "\\":
            i += 2
            continue
        if quote:
            quote = None if c == quote else quote
        elif c in "'\"":
            quote = c
        elif line.startswith(("&&", "||"), i) or c == ";" or (c == "&" and line[i - 1:i] not in (">", "<")
                                                              and line[i + 1:i + 2] != ">"):
            parts.append(line[start:i])
            i += 2 if line.startswith(("&&", "||"), i) else 1
            start = i
            continue
        i += 1
    return parts + [line[start:]]


def _statements(command: str) -> list[str]:
    """Shell statements in `command`; a heredoc body or a backslash-continued line stays with its first line."""
    out, lines, i = [], command.split("\n"), 0
    while i < len(lines):
        block, heredoc = [lines[i]], HEREDOC.search(lines[i])
        i += 1
        while (heredoc or block[-1].endswith("\\")) and i < len(lines):
            block.append(lines[i])
            i += 1
            if heredoc and block[-1].strip() == heredoc.group(1):
                heredoc = None
        out += ["\n".join(block)] if len(block) > 1 else _split_unquoted(block[0])
    return [s for s in out if s.strip()]


def _in_wiki(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _retired(path: Path, roots: list[Path]) -> bool:
    kitchen_wiki = os.environ.get("KITCHEN_WIKI")
    in_scope = _in_wiki(path, roots) or (kitchen_wiki and path.parent == Path(kitchen_wiki).resolve())
    return path.name in RETIRED and "_archived" not in path.parts and bool(in_scope)


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
        here, paths, judged = Path(cwd).resolve(), [], []
        for stmt in _statements(args.get("command") or ""):
            words = set(WORD_BREAK.split(stmt)) | {w for seg in _split_into_segments(stmt) for w in seg}
            found = [_resolve(w, variables, cwd) for w in words if w]
            paths += found
            if (_in_wiki(here, roots) or any(_in_wiki(p, roots) for p in found)
                    or any(s in stmt for k, (v, root) in named.items() for s in _spellings(k, v, root, here))):
                judged += [stmt, os.path.expandvars(stmt), *(_read(p) for p in found if not _in_wiki(p, roots))]
        hit, text = bool(judged), "\n".join(judged)
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
