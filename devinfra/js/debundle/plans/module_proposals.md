# Graph Planner Follow-Ups

Design follow-ups for <../TODO.md>, not a separate priority queue. Keep
`debundle modules propose` corpus-neutral; downstream findings stay in the
downstream repository. The graph model is in <../docs/design.md> § Valid peels
and atomic modules, and the current proposer contract is in
<../docs/peel_proposer.md>.

Extend the existing command. Corpus taxonomy is consumer policy, not
Debundler core logic.

- **Proposal metadata.** `ModuleProposal` already reports size, source line
  range, ordinal span, graph-edge counts, and `landability_notes` for specific
  manual-handling cases such as residual dependencies and unsupported
  import/export shapes. Still open: source-locality scores, likely naming
  anchors, and broader warnings for graph-valid proposals that look awkward to
  maintain.
- **Large function-internal seams.** Source-unit proposals cannot split a
  giant handler into independent semantic branches. Until a finer-grained model
  exists, report this granularity limit instead of suggesting arbitrary
  source-unit moves.
