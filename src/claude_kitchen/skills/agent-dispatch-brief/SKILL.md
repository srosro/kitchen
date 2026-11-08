---
name: agent-dispatch-brief
description: Use only when the user explicitly wants a brief or handoff for another agent, or asks to convert a plan, spec, bug report, PR finding, or request into agent-ready implementation instructions. Produces objective, context, allowed file touch set, out-of-scope items, acceptance criteria, validation commands, risk notes, dependencies, and human judgment needed. Does not file issues, dispatch agents, or implement code.
---

# Agent Dispatch Brief

Turn ambiguous source material into a narrow implementation brief that another coding agent can execute safely.

## Priority

- `§ Workflow`
- `§ Acceptance Criteria`
- `§ Allowed File Touch Set`
- `§ Human Judgment Needed`
- `§ Safety Defaults`

## Use This Skill When

- A user wants a messy plan, spec, bug report, PR finding, or request converted into agent-ready work.
- The desired output is a handoff brief, not code changes.
- Work should be scoped tightly enough for one implementation pass.
- The brief may later feed an issue tracker or agent dispatcher.

## Do Not Use For

- Filing issues or splitting a roadmap into tickets.
- Running, managing, or reviewing dispatched agents.
- Rewriting the underlying spec unless the user asks for content work.
- Bundling unrelated tasks into one large brief.

## Workflow

1. Extract the real objective: what should be true after the work is done.
2. Preserve useful source context: repo facts, symptoms, evidence, constraints, prior decisions, and links or file references.
3. Scope the smallest coherent task. If the source contains multiple independent changes, recommend separate briefs.
4. Define the allowed file touch set. Prefer exact paths. If edit paths are unknown, make the first brief read-only discovery and require the implementing agent to propose a touch set before editing.
5. Convert expectations into observable acceptance criteria. Avoid vague criteria such as "works well" or "clean up."
6. Pick validation commands from the project when available. If commands are unknown, say how to discover them rather than inventing commands.
7. Surface risks, dependencies, and decisions that require a human.
8. Keep the result generic and safe: no secrets, private credentials, proprietary assumptions, or unnecessary internal identifiers.

Ask at most three clarifying questions only when a missing fact changes scope or safety. Otherwise, state assumptions directly in the brief.

## Brief Template

```md
# Agent Brief: <short imperative title>

## Objective
<One or two sentences describing the exact outcome.>

## Context
- <Relevant source facts, symptoms, links, logs, review findings, or repo constraints.>
- <Use file/line references when known.>

## Allowed File Touch Set
- May edit: <specific files/directories only; if unknown, this is discovery-only and no edits are allowed yet>
- May read: <useful context paths, docs, tests, or related modules>
- Do not touch: <explicitly excluded files/directories/behaviors>

## Out of Scope
- <Nearby work the agent must not perform.>

## Acceptance Criteria
- <Externally observable behavior or code state.>
- <Regression that must remain unchanged.>

## Validation Commands
- `<command>` - <what it proves>
- If not known: <how to find the project-standard validation command>

## Risk Notes
- <Compatibility, migration, performance, security, UX, data, or release risks.>

## Dependencies
- <Blocked inputs, upstream PRs, credentials, services, fixtures, or decisions.>

## Human Judgment Needed
- <Product, design, policy, copy, rollout, or tradeoff decisions a human must make.>

## Agent Instructions
- Keep the change limited to the allowed touch set.
- Follow existing project patterns and tests.
- Do not perform unrelated refactors or cleanup.
- Report changed files, validation run, and any unvalidated risks.
```

## Quality Bar

- The objective is one implementation unit, not a theme.
- Every allowed edit path has a reason. Do not over-grant broad write access.
- Acceptance criteria describe evidence, not intent.
- Validation commands are copy-pasteable when known and minimal for the risk.
- Suggested implementation details are labeled as suggestions unless they are requirements.
- Out-of-scope items are explicit enough to prevent enthusiastic overreach.
- Human judgment is not hidden inside acceptance criteria.

## Safety Defaults

- Do not include secrets, tokens, customer data, or private environment values in the brief.
- Do not ask an agent to use destructive commands unless the source explicitly requires it and a human approves.
- Do not require network access, paid services, or credentials unless they are true dependencies.
- When repository context is dirty or shared, tell the agent to avoid reverting unrelated work.
