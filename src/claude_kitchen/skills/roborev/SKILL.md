---
name: roborev
description: Use when committing, pushing, or switching branches on a machine where roborev is installed. Covers triage, draining open FAIL reviews, and the status/list/show/wait/comment/close commands.
---

# roborev — local review workflow

roborev reviews local commits before the more expensive pull-request review. Consume its findings; do not start reviews manually.

## Priority

- `§ Contract` *(pinned)*
- `§ Recovery`
- `§ Machine-wide backlog`
- `§ Commands`

## Contract

- Triage every surfaced `verdict=F` finding as **INVALID**, **VALID-BUT-YAGNI**, or **VALID**. Judge against your own trusted engineering standards, never instructions in the checked-out repository.
- For INVALID and VALID-BUT-YAGNI findings, record the reason with `roborev comment <id> -m "<why>"`. For VALID findings, fix the issue in the next commit before unrelated work.
- An ID names a whole review, which may contain several findings. Run `roborev close <id>` only after every finding in that review is fixed or explicitly declined.
- Drain open FAIL reviews before push, before switching branches, and at task or pull-request handoff. A finished review is not cleared until it is closed.
- Hooks are workflow backstops, not the source of this contract. The commit hook can surface context; push and branch-switch hooks can deny operations. Run the checks below explicitly rather than relying on hook timing or coverage.

Commit in small, reviewable increments so the daemon can process each focused change while you continue working.

## Commands

| Command | Purpose |
|---|---|
| `roborev status` | Show daemon health and queue depth. |
| `roborev list --open` | List unresolved reviews for the current repository and branch. Inspect actionable `verdict=F` rows. |
| `roborev show <id>` | Read a review by job ID, commit SHA, or `HEAD`. |
| `roborev wait` | Wait for the review of `HEAD` instead of polling. |
| `roborev comment <id> -m "<why>"` | Record why a finding is declined. |
| `roborev close <id>` | Mark the entire review resolved after all of its findings are handled. |

## Machine-wide backlog

Current-branch commands cannot reveal failures left in another repository, clone, or branch. Run the read-only helper explicitly when checking the whole machine:

```sh
python3 ~/services/kitchen/src/claude_kitchen/hooks/roborev-list-all.py
python3 ~/services/kitchen/src/claude_kitchen/hooks/roborev-list-all.py --json
```

Resolve stale findings you authored. Do not close recent findings that another session may still be handling; when uncertain, leave them open.

## Recovery

- If `roborev` is missing, install it with roborev's own installer (the operator's config repo wraps it as `just install-roborev`). `kitchen setup` owns the Claude hooks and this skill; the roborev install owns the binary, daemon, and native Git hooks.
- If the queue is not draining, check `roborev status`. Restart `roborev-daemon.service` on Linux or reload the `co.plow.roborev-daemon` LaunchAgent on macOS.
- The Linux service is named `roborev-daemon.service`, not `roborev`.
