# Review instructions — kitchen

**Operating point:** a general-purpose agent harness used by a small team.
Iteration speed and clear extension seams matter more than hypothetical scale.

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
