#!/usr/bin/env bash
# PostToolUse[AskUserQuestion] hook. Nudges the agent to capture the human's
# answer as a durable `feedback` memory. The model already has the Q&A in its
# context, so we don't parse stdin — we drain it and emit a fixed reminder.
# This sidesteps any payload-schema fragility and keeps the hook trivial.
set -euo pipefail
cat >/dev/null 2>&1 || true   # drain stdin so the harness's writer never EPIPEs
cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"CAPTURE CHECK: You just asked the human a question and received their answer. Every question you had to ask is a decision you could not make autonomously — its answer is a candidate durable rule that would let you stop asking next time. If this answer is a reusable directive (not a one-off), write it now to THIS project's memory/ as a feedback memory: capture the generalized principle/common pattern, with **Why:** and **How to apply:** lines. For evidence prefer a paraphrased summary or a PR#/file:line reference over a raw quote — omit any secret, credential, private URL, or PII entirely (use …*** below 8 secret characters and last-3 otherwise if you must reference one). If it is genuinely one-off, skip."}}
JSON
