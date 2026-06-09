# loom — interpolating prediction-market marginals into rollouts

Status: **program charter + first plan** (2026-06-09). No code yet; M0 defines
the contracts.

## Mission

Prediction markets price **marginals** — `P(S&P ≥ X by date Y)`,
`P(OpenAI IPO by 2028)` — but a downstream simulator needs **worlds**: coherent
joint trajectories you can sample. loom converts a curated set of market quotes
plus historical evidence into **weighted sets of realistic world trajectories
whose implied marginals match the market prices**:

- **dense series** — e.g. a continuous monthly S&P level path per rollout;
- **discrete event streams** — e.g. funding rounds / tender / IPO / collapse
  with valuations — that satisfy structural validity (a world where OpenAI
  collapsed cannot later contain its IPO).

augur is the first consumer, not the owner: loom emits a serialized
**WorldSet** artifact; augur reads it through a thin bridge on augur's side.

## Inherited thinking (decided; do not re-derive)

This program executes a position already worked out in augur. Read these before
touching design:

- `finance/augur/plans/interpolating_prediction_markets.md` — the operation is
  a **soft min-KL projection**: sample a base measure `Q` (where the coupling
  lives), then reweight/exponentially tilt so the empirical marginals land on
  the market prices under a weighted divergence. Soft, because real market sets
  are mutually incoherent (threshold/time ladders violated across platforms);
  the objective degrades gracefully instead of failing.
- `finance/augur/plans/exogenous_rollout_architecture.md` — the layer split:
  **classical state-space for liquid macro** (fit on history, reweighted to
  markets), **authored parametric event programs for sparse/discrete things**
  (θ fitted by simulation moment-matching — _fit, don't reweight_, because a
  reweight can never lift mass `Q` never proposed), and macro-conditioned event
  generation so the macro/event joint comes for free, no copula.
- `finance/augur/x/pm_reifier/README.md` — spike verdicts: an LLM-as-`Q` works
  mechanically but is under-dispersed (**coverage, not calibration, is the
  binding constraint**); per-step kernels beat one-shot dense emission; the
  structured state-space baseline was better-calibrated on the same scorecard.
  **The LLM stays out of the hot path** — admissible later only as an
  _author_ of event-program structure, gated by classical fitting/validation.

Standing design commitments imported from those docs: coverage/ESS as hard
gates; soft weighted matching with optional isotonic ladder repair; validation
is three separate honest things (reproduction of the marginals, trajectory
sanity, and accumulated skill on resolved markets).

## Coupling contract with augur

| rule                    | concretely                                                                                                                                                |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| dependency direction    | `//loom/...` never depends on `//finance/augur/...`                                                                                                       |
| integration surface     | the **WorldSet artifact** — a data contract, not code                                                                                                     |
| bridge lives augur-side | a target under `finance/augur/` imports both and maps WorldSet → `SampledExogenousBundle` / PE bundle channels                                            |
| migrations are atomic   | generic pieces that move out of augur (market platform clients, hazard machinery, eval scorecards) move in single atomic PRs flipping augur's imports too |
| public/private boundary | loom is generic and public (ducktape); curated private catalogs, holdings-adjacent config, and deployments stay in gaffer-private (augur's existing rule) |

## Core contracts (M0)

- **WorldSet** — the artifact: a directory of parquet tables + `manifest.json`.
  - `series.parquet` — dense monthly levels:
    `(rollout_index, month_index, series_id, value)`.
  - `events.parquet` — discrete events:
    `(rollout_index, month_index, process_id, event_type, payload…)`.
  - `weights.parquet` — per-rollout log-weight (uniform pre-fit).
  - `manifest.json` — provenance + diagnostics: catalog with price-as-of per
    market, base-measure id/config, anchor date + spots, fit config, per-market
    residuals, ESS, sanity-gate results.
- **MarketConstraint** — typed binding of one market to a measurable functional
  of a world (`threshold_at_date`, `event_by_date`, `bucket_family`, …) that
  compiles to an indicator column over a WorldSet. Shape ported from augur's
  `calibration/catalog.py` + `resolvers.py`, but defined against the neutral
  schema.
- **BaseMeasure** — protocol: `sample(horizon, n, seed) → WorldSet`
  (unweighted).
- **EventGrammar** — a typed state machine per discrete process (e.g.
  `private → {ipo, collapsed, acquired}`, absorbing): generation is valid **by
  construction**, and the same grammar **validates foreign WorldSets** (any
  LLM- or human-authored world gets gated — the "no IPO after collapse" class
  of sanity checks lives here, once, not per-model).
- **Fitters** — (a) exponential-tilt reweighter for dense constraints; (b)
  simulation moment-matching θ-fit for event programs.
- **Diagnostics** — residual table, ESS, which-constraints-bound; written into
  the manifest and rendered as a human-readable report.

## Milestones

**M0 — contracts + toy end-to-end (first code PR).** Package skeleton, the
contracts above, the tilt reweighter, diagnostics. Toy: a GBM cloud + 3
synthetic threshold markets with one deliberately incoherent pair → the soft
fit lands a least-divergence compromise; residuals + ESS reported. No augur
imports. Gate: `bbr test //loom/...` green.

**M1 — real dense macro pipeline.** Evidence-fit base measure over monthly
log-returns (reading the augur-evidence checkout the way augur's `fit/` does —
shared _data_, not shared code); a real public catalog (S&P / BTC / CPI
thresholds across Manifold / Polymarket / Kalshi); time-zero anchoring to live
spots (concept from `calibration/macro_anchors.py`); isotonic repair of
threshold/time ladders; market-quality weights; near-duplicate-market dedup
(many markets are functions of one latent — don't double-count). Migrate the
platform clients
(`finance/augur/calibration/{platform,manifold,polymarket,kalshi,quote,redis_cache,warm_cache,transient_retry}.py`)
augur → loom in one atomic PR. Gate: residuals within tolerance on the coherent
subset; ESS above threshold; held-out historical-tail PIT for base vs
reweighted.

**M2 — market snapshot + resolution store (parallel to M1).** The one piece the
position doc calls the valuable long-term investment and augur explicitly does
not do (prices are always fetched live): capture
`(market, price, liquidity, as-of)` snapshots and resolutions on a schedule,
accumulating the only dataset that can ever measure **skill** rather than
mimicry. Default: extend the augur-evidence scraper pattern (same git/S3
plumbing). Gate: daily snapshots landing; a resolved-market scoring report
runs end-to-end (even while n is tiny).

**M3 — discrete event programs.** Generalize augur's PE machinery into the
event grammar: hazards over the state machine (the generic
`cdf_anchors → monthly hazard + tail` mechanism from `private_equity_risk.py`),
macro-conditioned hazards/marks so events ride the dense paths, θ-fit via
black-box moment matching (CMA-ES or similar), and the macro-coupling strength
as an explicit authored knob with presets — never a fitted number nothing
constrained. Gate: an OpenAI-shaped toy process fitted to illustrative event
markets passes the gate stack (marginal match within ε, monotone ladders,
grammar validity).

**M4 — augur bridge.** Augur-side adapter: WorldSet →
`SampledExogenousBundle` (+ provider config selecting a WorldSet artifact
path). Gate: one augur product run end-to-end on a loom artifact, and augur's
calibration report on it agrees with loom's own residuals within Monte-Carlo
error.

**M5 — eval harness as a product surface.** Port the spike's scorecards
(PIT/rank histograms, moving-block-bootstrap CIs, CRPS vs random-walk /
structured baselines) into `loom/eval`; every WorldSet ships with a validation
report (reproduction / sanity / skill-so-far). Later: reference-class fitting
for event programs (censoring-aware — time-rescaling, valuation PIT,
IPCW-Brier — per the architecture note).

Sequencing: M2 runs parallel to M1; M3 needs M1's dense paths; M4 can land
dense-only after M1 and pick up events after M3.

## What loom is not

- **Not trading or market-making.** "Coherence" means "a joint with these
  marginals exists", never no-arbitrage; loom never prices or trades.
- **Not a simulator.** Deterministic mechanics (tax, waterfalls, user policy)
  stay in augur's `sim`.
- **Not (yet) a skill claim.** Matching prices is agreeing with the crowd by
  construction; skill only accrues through M2's resolution store.

## Open decisions (defaults chosen; cheap to reverse before M1)

1. **Name + home**: `loom/`, top-level. Renaming is trivial until M1 code
   lands.
2. **Platform-client migration timing**: during M1 (when loom first needs live
   quotes), not in the M0 skeleton.
3. **Artifact layout**: directory of parquet + `manifest.json` (polars-native,
   matches augur's frame stack); no archive container.
4. **Snapshot store backend**: extend the augur-evidence scraper vs a new
   store. Default: extend.

## Risks

- **Coverage/ESS collapse** — the spike's recurring failure mode. Mitigation:
  hard gates in every fit + fit-don't-reweight for anything sparse.
- **Correlated/near-duplicate markets** double-counting one latent. Mitigation:
  dedup/cluster weighting in M1; genuinely open research — watch the residuals.
- **Scope creep into rebuilding augur.** Mitigation: the dependency rule;
  anything deterministic or holdings-specific belongs to augur/gaffer-private.
- **Quiet divergence of duplicated code.** Mitigation: never copy from augur —
  migrate atomically or keep depending the allowed direction.
