---
name: zoom-out
description: Map an unfamiliar or risky area before changing it by summarizing docs, modules, callers, data flow, ownership, tests, constraints, and the smallest safe next action. Use when the user says "zoom out," asks how code fits together, asks for architecture context, or when implementation would otherwise start from partial understanding.
---

# Zoom Out

Pause implementation and move one level up.

Use this skill to build a map that constrains the next edit. It is not a license to do a broad refactor.

## Priority

- `§ Workflow`
- `§ Output Shape`
- `§ Investigation Rules`
- `§ When To Use`

## When To Use

- The user explicitly says "zoom out" or asks how an area fits together.
- The change touches multiple layers, product behavior, persistence, routing, or UI/data boundaries.
- The repo is unfamiliar, has duplicate paths, or has fragile tests.
- A prior fix failed because the owner, contract, or data flow was misunderstood.
- You need to brief another agent or review a risky change.

Skip it for narrow read-only questions where the owning file and behavior are already clear.

## Workflow

1. Establish the working state when implementation may follow:
   - repo root
   - branch
   - `git status --short`
   - whether isolated work is needed before edits
2. Read the governing context before code:
   - local agent instructions
   - README and package docs
   - specs, ADRs, runbooks, and current-state docs
3. Map the code:
   - entry points
   - core modules and responsibilities
   - callers and callees
   - data/control flow
   - truth owners and generated or derived state
   - public contracts, interfaces, schemas, or view models
   - tests and fixtures
   - known constraints or ownership rules
   - duplicate or legacy paths that must not both be changed blindly
4. Identify the behavioral contract:
   - what the user sees
   - what data is authoritative
   - where validation happens
   - what must not regress
5. Summarize the map in plain language.
6. Identify the smallest safe next action, validation command, and risks that remain.

## Investigation Rules

- Prefer targeted search and file reads over broad browsing.
- Read callers before editing shared helpers.
- Distinguish product behavior from tests, eval scaffolding, reporting, and docs.
- Do not infer architecture from filenames alone. Verify call sites.
- Do not patch around symptoms until the owning layer is clear.
- Do not start refactoring during zoom-out unless the user explicitly asks.
- If the map shows unrelated dirty work, recommend isolated work before implementation.

## Output Shape

```md
**State**
- ...

**Map**
- ...

**What matters**
- ...

**Boundaries**
- ...

**Tests**
- ...

**Risks**
- ...

**Next step**
...
```

For a handoff-ready brief, add:

```md
**Implementation Brief**
- In scope:
- Out of scope:
- Likely files:
- Validation:
- Open questions:
```
