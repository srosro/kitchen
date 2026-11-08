#!/usr/bin/env bash
# UserPromptSubmit hook. Nudges the agent to capture an inline human directive
# (correction / preference / override typed in chat) as a `feedback` memory.
# Fires on every prompt; the reminder is conditional so ordinary task requests
# are a no-op. Model has the message in context, so we don't parse stdin.
set -euo pipefail
cat >/dev/null 2>&1 || true   # drain stdin
cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"CAPTURE CHECK: If the message above is a correction, directive, override, or stated preference about HOW you should work (not an ordinary task request or question), capture it now to THIS project's memory/ as a feedback memory: record the generalized pattern, with **Why:** and **How to apply:** lines. Encode the spirit so it fires on the next different instance, not a verbatim repeat. For evidence prefer a paraphrased summary over a raw quote — omit any secret, credential, private URL, or PII entirely (use …*** below 8 secret characters and last-3 otherwise if you must reference one). Otherwise, skip."}}
JSON
