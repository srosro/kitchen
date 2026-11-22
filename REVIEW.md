# Review instructions — kitchen

## Cause-first bug fixes

Fix at the cause; never with a workaround that leaves the cause in place. A guard or check at the boundary where the cause lives is a cause-level fix. If a fix adds a maintained path or net complexity, explain why removing the cause directly is insufficient. No stopgaps: they promote brittle, confusing, unreliable code. Ship the fix at the cause. When the bug comes from a duplicated source or identity, remove the duplicate rather than bridging the two. Limits a REVIEW.md already records as accepted stay out of review unless the diff widens them.

**Operating point:** a general-purpose agent harness used by a small team.
Iteration speed and clear extension seams matter more than hypothetical scale.

**Sibling repos:** `plow-pbc/kitchen`, the Rust replacement.

## Review bar

- **Simple, maintainable, extensible code wins.** Prefer one clear owner and a
  direct path over speculative abstractions, fallback chains, configuration no
  user asked for, or guards for states the harness cannot produce.
- **Brittleness costs more than compute.** Flag piles of regex and hand-rolled
  parsing that make behavior hard to extend. Prefer a clear model call for
  semantic work or a maintained standard parser for structured input.
- **Kitchen and Swarm stay loosely coupled.** Kitchen must remain a
  general-purpose agent harness, and Swarm must work with any client. They meet
  through open standards such as A2A and AI Catalog, never shared code or a
  private API.
