# Graph Planner Follow-Ups

Design follow-ups for <../TODO.md>, not a separate priority queue. Work on `debundle modules propose` stays
corpus-neutral: private downstream findings stay in the downstream repo. The graph model is in <../docs/design.md> §
Valid peels and atomic modules; the current proposer is in <../docs/peel_proposer.md>.

Extend the existing command rather than adding a parallel CLI: new information goes into proposal metadata, output stays
bounded JSON, and corpus taxonomy is consumer policy rather than debundler core logic.

- **Proposal metadata.** `ModuleProposal` carries size, source line range, ordinal span and cross-cell edge counts.
  Still missing: source-locality scores, likely naming anchors, and warnings when a proposal is graph-valid but likely
  awkward for humans.
- **Large function-internal seams.** A single giant handler can contain independent semantic branches that owner-level
  splitting cannot see. Needs a finer-grained model; until then planner output should say "blocked by function-internal
  granularity" instead of proposing arbitrary owner moves.
