#!/usr/bin/env bash
# UserPromptSubmit hook. Nudges the agent to capture an inline human directive
# (correction / preference / override typed in chat) as a feedback note in the
# wiki or repo its audience reads.
# Fires on every prompt; the reminder is conditional so ordinary task requests
# are a no-op. Model has the message in context, so we don't parse stdin.
set -euo pipefail
cat >/dev/null 2>&1 || true   # drain stdin
repo=$(sed -n 's/^AGENT_RULES_REPO=//p' "$HOME/.config/kitchen/operator.env" 2>/dev/null | tr -d "\"'") || true
rules="the operator's agent-rules repo"
[ -z "$repo" ] || rules="the $repo repo"
json=$(cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"CAPTURE CHECK: If the message above is a correction, directive, override, or stated preference about HOW you should work (not an ordinary task request or question), capture it now as a feedback note, never to Claude's auto-memory or a project memory/ dir. Route it by audience (PERSONAL_RUNBOOK, COMPANY_RUNBOOK and AGENT_RULES_REPO are in ~/.config/kitchen/operator.env): generalizable agent behavior → a PR to @RULES@; company-shareable → a page in $COMPANY_RUNBOOK; personal or operator-specific → $KITCHEN_WIKI/preferences.md in a kitchen session (KITCHEN_WIKI is set), otherwise the project's page under $PERSONAL_RUNBOOK/projects/: record the generalized pattern, with **Why:** and **How to apply:** lines. Encode the spirit so it fires on the next different instance, not a verbatim repeat. For evidence prefer a paraphrased summary over a raw quote — omit any secret, credential, private URL, or PII entirely (use …*** below 8 secret characters and last-3 otherwise if you must reference one). Otherwise, skip."}}
JSON
)
printf '%s\n' "${json//@RULES@/$rules}"
