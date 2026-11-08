#!/usr/bin/env bash
set -u
HERE=$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/assert.sh"
HOOKS="$HERE/../../src/claude_kitchen/hooks"
# capture-answer.sh — PostToolUse[AskUserQuestion]: valid JSON, exit 0, right event + nudge
out=$(printf '{"tool_name":"AskUserQuestion","tool_input":{}}' | "$HOOKS/capture-answer.sh"); rc=$?
assert_rc 0 "$rc" "capture-answer exits 0"
printf '%s' "$out" | jq -e . >/dev/null 2>&1; assert_rc 0 $? "capture-answer emits valid JSON"
assert_eq "PostToolUse" "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.hookEventName')" "capture-answer event name"
ctx=$(printf '%s' "$out" | jq -r '.hookSpecificOutput.additionalContext')
assert_contains "$ctx" "feedback memory" "capture-answer nudges a feedback memory"
assert_contains "$ctx" "Why:" "capture-answer asks for Why"
assert_contains "$ctx" "How to apply:" "capture-answer asks for How to apply"
assert_contains "$ctx" "paraphrased" "capture-answer prefers paraphrased evidence (no raw capture)"
assert_contains "$ctx" "omit any secret" "capture-answer says to omit secrets/PII"

# capture-prompt.sh — UserPromptSubmit: valid JSON, exit 0, right event + nudge
out=$(printf 'go with the simpler version' | "$HOOKS/capture-prompt.sh"); rc=$?
assert_rc 0 "$rc" "capture-prompt exits 0"
printf '%s' "$out" | jq -e . >/dev/null 2>&1; assert_rc 0 $? "capture-prompt emits valid JSON"
assert_eq "UserPromptSubmit" "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.hookEventName')" "capture-prompt event name"
ctx=$(printf '%s' "$out" | jq -r '.hookSpecificOutput.additionalContext')
assert_contains "$ctx" "feedback memory" "capture-prompt nudges a feedback memory"
assert_contains "$ctx" "paraphrased" "capture-prompt prefers paraphrased evidence (no raw capture)"

# Both scripts executable
[ -x "$HOOKS/capture-answer.sh" ]; assert_rc 0 $? "capture-answer is executable"
[ -x "$HOOKS/capture-prompt.sh" ]; assert_rc 0 $? "capture-prompt is executable"

# The roborev context bridge consumes the same registry before putting review
# text back into an agent's context. A newly registered shape must therefore be
# redacted here without a second hook-specific pattern list.
redacted=$(python3 - "$HOOKS" <<'PY'
import importlib.util
import pathlib
import sys

hook = pathlib.Path(sys.argv[1]) / "roborev-pre-commit-context.py"
spec = importlib.util.spec_from_file_location("roborev_pre_commit_context", hook)
module = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(hook.parent))
spec.loader.exec_module(module)
fake_key = "sk_" + "live_" + "A" * 24  # assembled so no key-shaped literal sits in the repo
print(module._redact_secrets(f"review quoted {fake_key}; password=abcd\n-----BEGIN PRIVATE KEY-----\nabcd\nefg"))
PY
)
assert_not_contains "$redacted" "sk_live_$(printf 'A%.0s' {1..24})" "roborev context: shared Stripe shape is redacted"
assert_contains "$redacted" "…AAA" "roborev context: redaction retains only the final three characters"
assert_not_contains "$redacted" "…bcd" "roborev context: short assigned secrets reveal no credential characters"
assert_not_contains "$redacted" "…efg" "roborev context: short multiline PEM bodies reveal no credential characters"

# All three roborev hooks share the Claude Bash-request envelope decoder. The
# context bridge is silent when the optional roborev binary is absent on a base
# install; absence is not a broken installation.
python3 - "$HOOKS" <<'PY'
import contextlib
import importlib.util
import io
import pathlib
import sys

hooks = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(hooks))
from _roborev_hooklib import _read_bash_request

sys.stdin = io.StringIO('{"tool_name":"Bash","tool_input":{"command":"git push"},"cwd":"/tmp"}')
assert _read_bash_request() == ("git push", "/tmp")
sys.stdin = io.StringIO('{"tool_name":"Read","tool_input":{}}')
assert _read_bash_request() is None

hook = hooks / "roborev-pre-commit-context.py"
spec = importlib.util.spec_from_file_location("roborev_pre_commit_context", hook)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module._find_roborev = lambda: None
sys.stdin = io.StringIO('{"tool_name":"Bash","tool_input":{"command":"git commit -m x"},"cwd":"/tmp"}')
stdout = io.StringIO()
with contextlib.redirect_stdout(stdout):
    assert module.main() == 0
assert stdout.getvalue() == ""
PY
assert_rc 0 $? "roborev hooks: shared request decoder and optional-binary silence"

# A clear current branch allows silently. Machine-wide backlog visibility is an
# explicit operator action (`roborev-list-all.py`), not context injected into
# every unrelated push.
python3 - "$HOOKS" <<'PY'
import contextlib
import importlib.util
import io
import pathlib
import sys

hooks = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(hooks))
import _roborev_hooklib

_roborev_hooklib.open_fail_backlog = lambda: [
    {"repo": "other", "root_path": "/tmp/other", "branch": "stale", "id": 7}
]
hook = hooks / "roborev-pre-push-gate.py"
spec = importlib.util.spec_from_file_location("roborev_pre_push_gate", hook)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module._read_bash_request = lambda: ("git push", "/tmp/current")
module._resolve_repo_cwd = lambda *_: "/tmp/current"
module._inside_git_repo = lambda *_: True
module._current_branch = lambda *_: "main"
module._git_toplevel = lambda *_: "/tmp/current"
module._find_roborev = lambda: "/tmp/roborev"
module._list_jobs = lambda *_: []
stdout = io.StringIO()
with contextlib.redirect_stdout(stdout):
    assert module.main() == 0
assert stdout.getvalue() == ""
PY
assert_rc 0 $? "roborev pre-push: clear branch does not inject machine-wide backlog"

roborev_skill=$(cat "$HERE/../../src/claude_kitchen/skills/roborev/SKILL.md")
assert_not_contains "$roborev_skill" "surfaces this same backlog automatically" "roborev skill: no automatic backlog claim"
assert_not_contains "$roborev_skill" "the commit blocks" "roborev skill: no false close-and-commit claim"

assert_summary
