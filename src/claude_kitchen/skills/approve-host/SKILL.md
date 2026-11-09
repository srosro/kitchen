---
name: approve-host
description: Use when you answer for an agent's owner and that agent wants to contact a new outside host (its Swarm `a2a call` is waiting for approval or failed `not_approved`), and you decide to allow it.
---

# Approve an outside host for an agent

Decide as the owner would: allow only what the agent's stated purpose needs.

1. Run `kitchen approve-host @name:server host` on the machine where the agent's
   Swarm account lives. A call waiting for approval goes through within seconds,
   and later calls to that host don't ask. It refuses the operator's customer
   agents (`CUSTOMER_AGENTS` in `~/.config/kitchen/operator.env`) and accounts
   that have never called out from that machine.
2. Log it. The command prints JSON with `room` and `line`. Join `room` and send
   `line` there. If the join is refused (that room is the agent's and its
   owner's), send `line` to the room where you report to the owner, and say in
   your reply which room got it.

A hand-off of the agent to a new owner drops every approval, yours included.
