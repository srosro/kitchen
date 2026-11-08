# agent-dispatch-brief

The skill for turning "fix this" into work another agent can actually ship.

`agent-dispatch-brief` takes a messy request, plan, bug report, spec, or review finding and turns it into a narrow implementation brief.

It is not a coding skill. It is a delegation skill. The point is to make the work small enough to do, bounded enough to review, and clear enough that the next agent does not have to invent the assignment.

## What It Changes

Vague delegation creates vague diffs.

If you send an agent into a repo with "fix onboarding" or "clean up this PR feedback," it has to guess the target, the files, the definition of done, and the test plan. Some agents guess politely. Some agents redecorate the whole house.

`agent-dispatch-brief` removes the guesswork before the handoff. It writes the brief you wish every agent had received before it started touching code.

## What It Produces

- Objective: what the agent is trying to accomplish.
- Context: the facts the agent needs before acting.
- Allowed file touch set: where edits may happen.
- Out-of-scope items: tempting work that should not happen.
- Acceptance criteria: what must be true when the work is done.
- Validation commands: how the agent proves the change.
- Risk notes: what could break or expand.
- Dependencies: what the agent needs before starting.
- Human judgment needed: decisions that should not be guessed.

## Use It When

- You want to send work to another agent.
- A bug report is too messy to implement directly.
- A plan contains several tasks and needs one narrow slice.
- A PR review finding needs a safe implementation brief.
- You want to prevent scope creep before it starts.
- You need a repeatable handoff format for multi-agent work.

## Example Prompts

```text
Use $agent-dispatch-brief to turn this bug report into a brief for another coding agent.
```

```text
Use $agent-dispatch-brief on this plan. Keep the first implementation slice small and testable.
```

```text
Turn this PR review finding into an agent-ready fix brief with acceptance criteria and validation commands.
```

## Good Output Feels Like

A good brief is calm, specific, and a little strict.

It should make the next agent's job obvious:

- what to change
- what not to change
- where to work
- how to know it is done
- how to prove it
- where to stop and ask for human judgment

If an agent can read the brief and immediately begin by inspecting the right files, the brief is doing its job.

## What It Prevents

Agents need constraints. Without them, they expand scope, touch too many files, and report success without proving the right thing changed.

This skill makes the work small enough to hand off and specific enough to review. It prevents mystery boxes, giant opportunistic rewrites, invented requirements, and "fixed it" reports with no evidence.
