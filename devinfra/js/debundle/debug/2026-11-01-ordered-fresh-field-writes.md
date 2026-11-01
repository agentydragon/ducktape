# Ordered local field writes

The current implementation slice admits a static field assignment only after
the ordered local interpreter establishes a singleton, settled ordinary record
created in that invocation and an existing own data descriptor. It uses the
canonical source index and shared allocation heap. No new author premise,
provider summary, purity rule or validator cutover is included.
Existing certified record writes, array-iteration writes and writes through
unrebound parameters retain their established execution path. Moving those
proof obligations to the ordered interpreter is a remaining migration step.

Source review covered receiver-before-RHS evaluation, local rebinding, aliases,
self-links, allocation-site joins, TDZ/const errors, and conservative publication
when a field proof fails. This is code-review evidence, not a reproduction or
canonical program acceptance result. No tests were added or run locally;
existing hosted PR checks provide the next validation boundary.

## Encountered tooling friction

- **Addressed in this slice:** invocation fallback previously recorded only a
  dummy span. Field-write fallback now carries the actual assignment span
  through the callback operation and both precision-loss outcomes. Existing
  limit-triggered callers keep their existing span behavior.
- **F4 — Explain local-body admission:** `LocalBody::lower` discards unsupported
  syntax with `None`. During this implementation, understanding why a callable
  missed the ordered path required reading lowering and caller guards. Add a
  typed, bounded per-callable explanation identifying the first unsupported
  operation or failed runtime guard, with source position and report path.
  Distinguish syntax coverage from effect completion and imported-provider
  admission. Preserve the conservative fallback; a diagnostic must not grant
  a proof or rerun analysis.
- **F5 — Distinguish allocation generations:** the heap keys records by source
  allocation site. Repeated calls can conflate a new record with an older input
  or external alias from that site. This slice conservatively refuses the field
  proof for sites already represented in the heap or reachable through inputs
  or observed external binding reads. Derive distinct invocation/allocation
  identities in the shared domain before broadening that guard; source shape
  or occurrence of the literal in this frame alone cannot prove freshness.
