# Guyton–Klinger experiment implementation

Proposed STUDY consumer, not an implemented reproduction. The
[source contract](README.md) owns primary evidence, study versions,
the declared three-sleeve adaptation and remaining convention decisions. This plan
owns the proposed code and leaves as its slices land; the [roadmap](../../plans/roadmap.md) owns
cross-component dependencies.

## Composition on current Augur primitives

- <run.py> drives the annual windows of <paths.py> through a fresh <policy.py>
  `Policy` per run and per selected replay, and its CLI writes each path's
  `YearRecord`s. Trinity's sales-only funding, coupon cash and success boundary
  are **not** GK defaults.
- <../../sim/observations.py> supplies current lots/prices/basis, cash, CPI and typed
  receipts; exact sell/buy/consume actions own canonical settlement. Policies
  see observations, never the prepared future path arrays.
- <../../sim/results.py> retains compact payments/holdings and selected detailed
  receipts/books. <../../x/joint_spending_allocation/policy.py> demonstrates separate
  author-owned intention records; study measurements must not replay accounting.

## Next independently reviewable slices

1. **Historical report:** retain original/reordered path identity, source and
   unconditional spending metrics; run a pinned sourced panel for the declared
   adaptation and explain substitutions/discrepancies. Keep source acquisition
   separate from offline CI.
2. **Optional 2006 stochastic comparison:** reconstruct the explicit joint annual
   model and compare the named tables with sampling uncertainty. Different random
   paths are acceptable; silently different distributions/rules are not.

Later controls: cash return and no double-counted payouts; exact exhaustion versus $1
terminal success; failed-path exclusion from source medians but inclusion of
known paid zeros in observed populations. Use independently calculated amounts,
not snapshots copied from the implementation. None requires a separate financial simulator.
