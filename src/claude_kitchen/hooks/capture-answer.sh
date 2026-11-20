#!/usr/bin/env bash
# PostToolUse[AskUserQuestion] hook. Nudges the agent to capture the human's
# answer as a durable note in the runbook its audience reads. The model already has the Q&A in its
# context, so we don't parse stdin — we drain it and emit a fixed reminder.
# This sidesteps any payload-schema fragility and keeps the hook trivial.
set -euo pipefail
cat >/dev/null 2>&1 || true   # drain stdin so the harness's writer never EPIPEs
repo=$(sed -n 's/^AGENT_RULES_REPO=//p' "$HOME/.config/kitchen/operator.env" 2>/dev/null | tr -d "\"'" | tail -n1) || true
rules="the operator's agent-rules repo"
[ -z "$repo" ] || rules="the $repo repo"
json=$(cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"CAPTURE CHECK: You just asked the human a question and received their answer. Every question you had to ask is a decision you could not make autonomously — its answer is a candidate durable rule that would let you stop asking next time. If this answer is a reusable directive (not a one-off), write it now as a feedback note, never to Claude's auto-memory or a project memory/ dir. Route it by audience (PERSONAL_RUNBOOK, COMPANY_RUNBOOK and AGENT_RULES_REPO are in ~/.config/kitchen/operator.env): generalizable agent behavior → a PR to @RULES@; how any agent does a task → the task-named company skill $COMPANY_RUNBOOK/skills/how-to-<task>/SKILL.md (edit the matching one or create it, its description starting \"Use when\"; never a page named for an agent); anything about one person (their preferences, household, accounts, voice) → your runbook, $PERSONAL_RUNBOOK/runbooks/<your agent's folder name>.md (a cook writes its sous's; create it if it doesn't exist), or outside a kitchen the project's page under $PERSONAL_RUNBOOK/projects/; never preferences.md or mistakes.md: capture the generalized principle/common pattern, with **Why:** and **How to apply:** lines. For evidence prefer a paraphrased summary or a PR#/file:line reference over a raw quote — omit any secret, credential, private URL, or PII entirely (use …*** below 8 secret characters and last-3 otherwise if you must reference one). If it is genuinely one-off, skip."}}
JSON
)
printf '%s\n' "${json//@RULES@/$rules}"
