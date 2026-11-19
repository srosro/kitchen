#!/usr/bin/env bash
set -u
HERE=$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/assert.sh"
HOOKS="$HERE/../../src/claude_kitchen/hooks"
RULES_HOME="$HERE/fixtures/rules-home"
# The agent-rules repo comes from operator.env; unset, the nudge names it generically.
for hook in capture-answer capture-prompt; do
  ctx=$(echo '{}' | HOME="$RULES_HOME" "$HOOKS/$hook.sh" | jq -r '.hookSpecificOutput.additionalContext')
  assert_contains "$ctx" "a PR to the acme/agent-rules repo" "$hook names AGENT_RULES_REPO from operator.env"
  ctx=$(echo '{}' | HOME="$RULES_HOME/none" "$HOOKS/$hook.sh" | jq -r '.hookSpecificOutput.additionalContext')
  assert_contains "$ctx" "a PR to the operator's agent-rules repo" "$hook without AGENT_RULES_REPO names the repo generically"
done

# capture-answer.sh — PostToolUse[AskUserQuestion]: valid JSON, exit 0, right event + nudge
out=$(printf '{"tool_name":"AskUserQuestion","tool_input":{}}' | "$HOOKS/capture-answer.sh"); rc=$?
assert_rc 0 "$rc" "capture-answer exits 0"
printf '%s' "$out" | jq -e . >/dev/null 2>&1; assert_rc 0 $? "capture-answer emits valid JSON"
assert_eq "PostToolUse" "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.hookEventName')" "capture-answer event name"
ctx=$(printf '%s' "$out" | jq -r '.hookSpecificOutput.additionalContext')
assert_contains "$ctx" "\$PERSONAL_RUNBOOK/projects/" "capture-answer routes personal notes to the personal runbook"
assert_contains "$ctx" "\$PERSONAL_RUNBOOK/runbooks/<your agent's folder name>.md" "capture-answer routes a personal learning to the agent's runbook"
assert_contains "$ctx" "\$COMPANY_RUNBOOK/runbooks/" "capture-answer routes a company practice to a company page"
assert_contains "$ctx" "never preferences.md or mistakes.md" "capture-answer no longer writes the shared kitchen wiki"
assert_contains "$ctx" "never to Claude's auto-memory" "capture-answer keeps notes out of auto-memory"
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
assert_contains "$ctx" "\$PERSONAL_RUNBOOK/projects/" "capture-prompt routes personal notes to the personal runbook"
assert_contains "$ctx" "\$PERSONAL_RUNBOOK/runbooks/<your agent's folder name>.md" "capture-prompt routes a personal learning to the agent's runbook"
assert_contains "$ctx" "\$COMPANY_RUNBOOK/runbooks/" "capture-prompt routes a company practice to a company page"
assert_contains "$ctx" "never preferences.md or mistakes.md" "capture-prompt no longer writes the shared kitchen wiki"
assert_contains "$ctx" "never to Claude's auto-memory" "capture-prompt keeps notes out of auto-memory"
assert_contains "$ctx" "paraphrased" "capture-prompt prefers paraphrased evidence (no raw capture)"

# Both scripts executable
[ -x "$HOOKS/capture-answer.sh" ]; assert_rc 0 $? "capture-answer is executable"
[ -x "$HOOKS/capture-prompt.sh" ]; assert_rc 0 $? "capture-prompt is executable"

# runbook-secret-gate.py: a write into either runbook wiki that carries a credential
# shape is denied; a clean write, or a secret written elsewhere, passes.
GATE_HOME=$(mktemp -d); trap 'rm -rf -- "$GATE_HOME"' EXIT
mkdir -p "$GATE_HOME/.config/kitchen" "$GATE_HOME/wiki/personal/projects" "$GATE_HOME/wiki/plowco"
ln -s personal/projects "$GATE_HOME/wiki/projects"
printf 'PERSONAL_RUNBOOK="~/wiki/personal"\nCOMPANY_RUNBOOK=~/wiki/plowco\n' > "$GATE_HOME/.config/kitchen/operator.env"
planted="gh""p_$(printf 'A%.0s' {1..36})"  # assembled so no token-shaped literal sits in the repo
gate() { # gate <tool> <tool_input json> -> the decision, or "allow" when the hook prints nothing
  local out; out=$(jq -nc --arg t "$1" --argjson i "$2" '{tool_name:$t,tool_input:$i}' | HOME="$GATE_HOME" "$HOOKS/runbook-secret-gate.py")
  [ -n "$out" ] && printf '%s' "$out" | jq -r .hookSpecificOutput.permissionDecision || echo allow
}
P="$GATE_HOME/wiki/personal/runbooks/ea.md" C="$GATE_HOME/wiki/plowco/runbooks/scrum.md"
assert_eq deny "$(gate Write "$(jq -nc --arg f "$P" --arg c "token: $planted" '{file_path:$f,content:$c}')")" "gate: Write of a secret to a personal runbook is denied"
assert_eq deny "$(gate Edit "$(jq -nc --arg f "$C" --arg c "$planted" '{file_path:$f,old_string:"x",new_string:$c}')")" "gate: Edit adding a secret to a company page is denied"
assert_eq deny "$(gate MultiEdit "$(jq -nc --arg f "$P" --arg c "$planted" '{file_path:$f,edits:[{old_string:"x",new_string:$c}]}')")" "gate: MultiEdit adding a secret is denied"
assert_eq deny "$(gate Write "$(jq -nc --arg f "$GATE_HOME/wiki/projects/kitchen/x.md" --arg c "$planted" '{file_path:$f,content:$c}')")" "gate: a symlinked alias into a wiki is still a wiki"
assert_eq deny "$(gate Bash "$(jq -nc --arg c "echo $planted >> ~/wiki/plowco/kitchen.md" '{command:$c}')")" "gate: Bash appending a secret under a ~ path is denied"
assert_eq deny "$(gate Bash "$(jq -nc --arg c "printf %s $planted > \$PERSONAL_RUNBOOK/runbooks/pa.md" '{command:$c}')")" "gate: Bash writing a secret via \$PERSONAL_RUNBOOK is denied"
assert_eq allow "$(gate Write "$(jq -nc --arg f "$P" '{file_path:$f,content:"the key lives in the vault item ea-mail (…AAA)"}')")" "gate: a clean runbook write passes"
assert_eq allow "$(gate Write "$(jq -nc --arg f "$GATE_HOME/elsewhere.md" --arg c "$planted" '{file_path:$f,content:$c}')")" "gate: a secret written outside the wikis is not this gate's business"
assert_eq allow "$(gate Bash "$(jq -nc --arg c "echo $planted > /tmp/x" '{command:$c}')")" "gate: a Bash command that touches no wiki passes"
assert_eq "" "$(printf 'not json' | HOME="$GATE_HOME" "$HOOKS/runbook-secret-gate.py")" "gate: a malformed request fails open"
assert_eq "" "$(jq -nc --arg f "$P" --arg c "$planted" '{tool_name:"Write",tool_input:{file_path:$f,content:$c}}' | HOME="$GATE_HOME/none" "$HOOKS/runbook-secret-gate.py")" "gate: a machine without the wiki settings is not gated"

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
