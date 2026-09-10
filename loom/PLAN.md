# loom Plan

Last trimmed: 2026-06-12.

The proposed `loom` pipeline converts prediction-market marginals plus dated
evidence into forecastable worlds. Augur is a prospective consumer, not the owner:
the proposed bridge maps generic artifacts into Augur's own bundles. These
pipeline proposals do not gate Augur's current experiments or override its
[model-adoption roadmap](../finance/augur/plans/roadmap.md).

## Current State

- `loom/gym/` is the implemented forecasting-eval substrate: task schema,
  proper scoring, asserted model cutoffs, series/path/bundle task generation,
  panel selection, run comparison, and the Inspect-based agent harness.
- `loom/wayback/proxy/` and `loom/wayback/cache/` provide dated web access
  for agent evals. Operational follow-ups live in `gym/TODO.md` and
  `wayback/cache/PLAN.md`.
- `loom/plans/market_harvest.md` is the source survey and pipeline design for
  the market-resolved task family.
- The WorldSet-producing pipeline is still plan-stage. No durable `WorldSet`,
  `MarketConstraint`, `BaseMeasure`, or `EventGrammar` implementation exists in
  `loom/` yet.

## Standing Decisions

- `//loom/...` does not depend on `//finance/augur/...`.
- The integration surface is a serialized WorldSet artifact; the Augur bridge
  lives under `finance/augur/`.
- Shared generic market/evidence code should move to shared packages in atomic
  PRs that update all callers. Do not copy Augur code into Loom.
- Classical macro paths with soft market reweighting and authored sparse-event
  programs are candidate approaches, not a required model family or exclusive
  authoring method. Evaluate their behavior against alternatives.
- Prediction-market prices are marginals, not trajectories. Loom supplies the
  coupling and validates the resulting joint paths.
- Matching market prices is reproduction, not skill. Skill only comes from
  proper scoring on resolved tasks or frozen past snapshots.

## Core Contracts To Build

- **WorldSet**: artifact directory with `series.parquet`, `events.parquet`,
  `weights.parquet`, and `manifest.json`. The manifest records market catalog,
  price-as-of inputs, base-measure identity/config, fit config, residuals, ESS,
  and sanity checks.
- **MarketConstraint**: typed binding from a market to a measurable functional
  over a WorldSet, such as threshold-at-date, event-by-date, or bucket family.
- **BaseMeasure**: protocol shaped like `sample(horizon, n, seed) -> WorldSet`
  before market fitting.
- **EventGrammar**: per-process state machine for sparse events. Generation
  should be valid by construction; validation should reject foreign artifacts
  with impossible transitions.
- **Fitters**: exponential-tilt reweighting for dense constraints and
  simulation moment matching for event-program parameters.
- **Diagnostics**: residual table, ESS, infeasible/all-zero constraints,
  grammar validity, monotone ladder checks, and rendered sample-world summaries.

## Proposed pipeline and separate eval work

These retain the earlier design, not a fresh dispatch or an Augur priority order.
Current gym/Wayback operations remain with the local trackers linked below.

1. **Market task harvesting.** Turn `plans/market_harvest.md` into the
   market-resolved gym family: Manifold backfill, Polymarket post-CLOB history,
   Kalshi forward capture, platform quality filters, and crowd baselines.
2. **Event/task schema split.** Separate durable events from forecast tasks.
   One event should support many `as_of` tasks and many marginal/joint queries;
   bundle tasks are the current approximation.
3. **WorldSet M0.** Add Loom package skeleton for WorldSet IO, a toy base
   measure, soft market fitting, diagnostics, and tests under `//loom/...`.
4. **Dense macro M1.** Fit monthly public series from dated evidence, compile
   real market constraints, repair or softly absorb incoherent ladders, and gate
   on residuals, ESS, and held-out tail coverage.
5. **Snapshot/resolution store M2.** Persist market price/liquidity snapshots
   and resolutions so future scoring can compare frozen Loom probabilities to
   market prices at the same `as_of`.
6. **Sparse event programs M3.** Generalize PE-style event processes into
   grammar-checked event programs with macro-conditioned hazards/marks and
   simulation moment matching.
7. **Augur bridge M4.** Add an Augur-side adapter from WorldSet artifacts to
   `SampledExogenousBundle` and product provider config. Gate with one product
   run and agreement between Loom and Augur calibration residuals.
8. **Gym contestants G2.** Compare the classical Loom pipeline, an
   agent-with-forecast-skill contestant, and the authored-program hybrid using
   paired proper-loss deltas over the same admissible task panel.
9. **Archive-backed eval reliability.** Finish the limiter telemetry rollout
   and panel rerun tracked in `gym/TODO.md`; keep the service design/status in
   `wayback/cache/PLAN.md`.

## Validation Norms

- Every artifact should report reproduction, trajectory sanity, calibration of
  invented dynamics, and skill-vs-crowd when resolved data exists.
- Use paired per-task deltas for contestant comparisons. Report aggregate
  metrics with cluster-bootstrap intervals by `as_of`.
- Treat correlated generated tasks as shared evidence, not independent samples.
  Routine LLM runs should use curated panels rather than the full grid.
- Keep leakage discipline physical where possible: dated stores, asserted model
  cutoffs, and sandboxed dated-web access.

## References

- `README.md`: concise overview and gym invocation.
- `gym/TODO.md`: active eval reliability and harness follow-ups.
- `gym/k8s/README.md`: in-cluster eval run procedure and operational gotchas.
- `wayback/cache/PLAN.md`: cache service design/status.
- `docs/archive_org_apis.md`: Internet Archive API behavior notes.
- `plans/market_harvest.md`: market source survey and harvest design.
- `../finance/augur/plans/market_model_research.md`: optional marginal-to-joint
  and sparse-company research questions, under Augur's existing adoption gates.
