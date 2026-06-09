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

## North star (reframe, 2026-06-09)

The terminal product is not trajectories — it is **answers**: "if I execute
strategy X, what is the distribution of outcome Y for me?" Those questions
cannot be prediction markets themselves (private information, no liquidity,
audience of one). So the system is judged as: **input** = dated past data +
market quotes of varying trustworthiness; **output** = a distribution for a
question; **metric** = proper loss against realized outcomes, with
training/eval tasks harvested from history.

Two consequences, joined by one decomposition:

- **"If I do X" is causal, and a loss metric cannot score counterfactuals**
  directly — the road not taken never resolves. The save is augur's existing
  boundary: for these questions the world is exogenous to the strategy (the
  S&P does not react to my rebalancing), so
  `P(Y | do X) = deterministic mechanics(X) ∘ P(exogenous world)`. The
  forecastable, scoreable object is the exogenous world; the personal
  conditional is a deterministic, unit-testable transform of it (`sim`).
  loom forecasts worlds; augur turns them into "my outcomes under X".
- **Trajectories are instrumental, not terminal.** They stay for three
  reasons: they are the substrate `sim` needs (the personal questions
  literally require them); a mechanistic decomposition is inspectable and
  enforces sanity (the EventGrammar class of checks); and in data-poor
  regimes — where the loss metric has no statistical power — inspectability
  is the only warrant available. Preference rule: **at equal loss, the more
  mechanistic answer wins**; the gym's verdicts on data-rich task families
  are what license trusting the same machinery on data-poor ones.

Personal-but-exogenous quantities (health events, job loss) are the same
epistemic object as the OpenAI module: a sparse private entity riding a dense
public world — reference-class shape, private covariates for location,
calibration measurable on the class, never on the individual.

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

## Modeling design

### Dense layer (liquid macro: S&P, BTC, CPI, home value / rent)

- **Base measure `Q`**: a joint monthly log-return model fit on the scraped
  evidence series — Student-t innovations (fat tails are the one property a
  reweight cannot add later), shrunk cross-series correlation, levels anchored
  at today's spots at month 0. Deliberately boring: the spike showed classical
  fitting ties or beats an LLM here, and what `Q` must supply is **coverage**
  (dispersion) and **coupling** (how series co-move); the markets supply the
  location of the marginals.
- **Constraint compilation**: each catalog market compiles to an indicator
  column over the sampled cloud. Pre-fit normalization: isotonic repair of
  threshold/time ladders; clustering of near-duplicate markets on one latent so
  a family of S&P thresholds counts as one source, not five.
- **The fit is a max-ent reweight (exponential tilt)**: find per-constraint
  duals `λ` so reweighted indicator means hit the prices —
  `w_i ∝ exp(Σ_m λ_m f_m(W_i))`; the dual is convex (Newton/L-BFGS). The soft
  form replaces equalities with per-market penalties weighted by quality `ω_m`
  (liquidity, horizon, platform), so mutually incoherent inputs land on a
  least-divergence compromise instead of failing.
- **When ESS collapses, move `Q`, not the weights**: an all-zeros indicator
  column means `Q` never proposed that region and no reweight can lift it. The
  escape hatch is moment-matching `Q`'s own drift/vol parameters toward the
  offending markets, then reweighting residually. Reweight-only stays the
  default because it preserves the historically fitted coupling exactly.

### Event layer (sparse discrete processes, e.g. OpenAI)

- **Model class**: a marked point process over the EventGrammar state machine
  with competing risks — per-transition hazards
  `λ_e(t | state, macro path, history)`, valuation marks as a log-space
  jump-diffusion partly driven by macro features. Each event process is
  generated **conditioned on an already-sampled dense trajectory**, so the
  macro/event joint comes from conditioning (risk-on lifts valuations and IPO
  hazard; a crash raises down-round/collapse hazard) — no copula.
- **Three kinds of parameter, three sources of truth**:
  - **Market-pinned**: event-timing term structure (the `P(IPO by d)` ladder →
    CDF anchors → piecewise hazard + tail) and valuation thresholds. Fit by
    simulation moment matching: roll out, score the indicator marginals,
    optimize θ black-box (CMA-ES or similar; θ is low-dimensional). _Fit,
    don't reweight_ — fitting moves real probability mass, so tail coverage is
    guaranteed.
  - **Reference-class-shaped**: everything no market prices (inter-round time
    shapes, valuation-step and down-round distributions, collapse rates) comes
    from fitting on many comparable past companies — empirical Bayes: the
    class fixes the shape, the entity's own history + markets fix location.
  - **Authored**: the macro-coupling strength is unidentifiable at n=1 (no
    observable constrains one company's joint with the S&P). It stays an
    explicit, surfaced knob with presets — a documented assumption, never a
    fitted number.

### Composition

One world = one dense draw → event programs run conditioned on it → rows in the
WorldSet. Weights come from the dense tilt; grammar validity holds by
construction; the validator re-checks every artifact, including
foreign-authored ones.

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
IPCW-Brier — per the architecture note). Under the reframe these scorecards
live inside the gym; the per-artifact report remains.

The gym track (see "Productized: the forecasting gym"), ordered eval-first →
bare baseline → skill techniques:

**G0 — gym core. ✅ landed (`loom/gym/`).** Task schema (binary + scalar
questions, info-cutoff `as_of`, realized outcome with provenance), proper-loss
scoring (log/Brier; pinball for stated quantiles), the asserted-cutoff model
registry (`model_cutoffs.py` — admissibility: model cutoff ≤ task `as_of`),
six hand-curated seed tasks, and the **bare-prompt structured-answer
baseline** (`//loom/gym:baseline_eval`, OpenAI-compatible endpoint; z.ai
coding tier — the free general tier 429s). First live glm-4.5 run over the
seed set: binary mean `log_loss` 0.50 / Brier 0.17; it under-predicted the
2024 bull run (p=0.40 on S&P ≥ 6000, p=0.65 on BTC ≥ 100k), consistent with
the spike's conservatism finding. Next: harvested task families
(series-derived from the evidence checkout, then market-resolved) to replace
hand-curation.

**G1 — market task harvesting.** Forward: the M2 snapshot/resolution store.
Backward: historical backfills (Manifold export, Kalshi/Polymarket history) so
the resolved-market task set isn't waiting on calendar time.

**G2 — contestants above the baseline.** (1) the classical pipeline once M1
lands; (2) the agent-with-forecast-skill contestant on asserted-cutoff models
— default z.ai `glm-4.5` (leakage-probed June-2024 cutoff; key provisioned
in-cluster as the claude-sandbox `zai-api-key` secret); (3) the authoring
hybrid once M3 lands. Each must beat G0's bare baseline on gym loss to
justify its machinery.

Sequencing: G0 lands alongside M0 — the metric exists before methods optimize
against it. M2 runs parallel to M1 and feeds G1; M3 needs M1's dense paths; M4
can land dense-only after M1 and pick up events after M3.

## How we know it's working

Four instruments, ordered weakest → strongest. Every WorldSet carries all four
in its manifest/report, so "are we doing a reasonable job" has a standing,
versioned answer per artifact instead of a vibe.

1. **Reproduction — necessary, never sufficient.** Per-market residual table
   (model-implied probability vs price, the compromise explicit where inputs
   were incoherent), ESS, and the infeasible-constraint list. Hard gates: ESS
   floor; no active all-zeros constraint. Catches broken fits; cannot catch
   agreeing with a wrong crowd or ugly paths.
2. **Trajectory sanity.** Grammar validity over `events.parquet`; monotone
   threshold/time ladders in the model's own implied marginals;
   non-negativity / no degenerate blowups; rendered fan charts plus a handful
   of fully-rendered sample worlds for eyeballing cross-series co-movement.
3. **Calibration of the invented dynamics — the main statistical test.**
   Markets pin a few coarse points; `Q` and the event programs invent
   everything between, and that part is testable on held-out history where
   outcomes are known. Dense: rolling-origin PIT/rank histograms with
   serial-dependence-robust verdicts (moving-block bootstrap — monthly PITs
   are autocorrelated, naive p-values lie), tail-escape rate for dispersion,
   CRPS vs a random-walk baseline for skill (baseline-relative scoring cancels
   era difficulty). Events: the same mindset ported to point processes —
   time-rescaling (rescaled inter-event gaps ~ Exp(1)), valuation-PIT at
   rounds, censoring-aware survival calibration (D-calibration, IPCW-Brier) —
   scored on held-out reference-class companies, because the target entity
   itself is n=1 and unresolved. The pm_reifier spike already proved this
   scorecard has teeth as a decision instrument: it rejected the LLM kernel
   (thin-tailed + biased) and passed the state-space tails on the same window.
4. **Skill vs the crowd — slow, and the only honest one.** From the M2
   snapshot store: when a market resolves, proper-score (log/Brier) our frozen
   probability-as-of-date against the outcome, side by side with the market's
   price as of the same date. On markets we fit to, a tie is expected by
   construction — the informative comparisons are quantities and dates the
   catalog did not pin. The end-to-end variant: rebuild a WorldSet as-of a
   past date purely from stored snapshots + evidence-as-of, then score
   realized history against it — the full-pipeline dress rehearsal.

The floor for "reasonable": beat the trivial baselines (random walk; the
untilted `Q`; Kaplan–Meier / constant hazard for events) on proper scores
while staying calibrated. Known-unvalidatable by design: the n=1
macro-coupling knob — surfaced and preset, because no data exists to fit it.

### Productized: the forecasting gym

Instruments 3–4 generalize into a standing eval harness — under the reframe,
the program's north-star deliverable:

- **Task** = `(as_of date t, question + resolution criterion, resolution
date, realized outcome)`; a contestant may use any data dated ≤ t.
- **Task families**: (a) **series-derived** — threshold/bucket/path-statistic
  questions manufactured from the evidence history; unlimited n, no scraping,
  the spike's PIT backtests recast as tasks; (b) **market-resolved** — from
  the M2 store going forward and historical dumps backward (Manifold full
  export; Kalshi/Polymarket historical APIs), with the market's price-as-of-t
  available both as a feature and as the crowd baseline; (c) **entity
  events** — reference-class companies viewed as-of past dates,
  censoring-aware scoring; (d) **end-to-end personal** — as-of WorldSet
  rebuild + `sim` vs our own realized history; tiny n, smoke test only.
- **Leakage discipline**: all data access through dated stores — the
  git-scraped evidence checkout is already an as-of index (`git checkout` at
  date t), M2 snapshots are dated rows; LLM-involved contestants run on
  **asserted-cutoff models** (weights released before t — the spike's
  leakage-probe methodology and candidate list); no undated sources inside
  eval runs.
- **Scoring**: log/Brier for binaries, CRPS/quantile loss for continuous,
  censoring-aware variants for events; always relative — vs price-as-of-t
  where a market exists (beating it = skill), vs climatology/random-walk
  where not; calibration reported separately from sharpness.
- **Contestants**: (1) the loom classical pipeline; (2) an agent running a
  forecast skill (superforecaster methodology + dated data tools), free-form;
  (3) the hybrid from the architecture note — the agent **authors** a
  mechanistic program, classical machinery fits and gates it. Which wins
  where becomes an empirical question, which is the point of the reframe.
- **Knobs become learnable**: market-quality weights `ω_m` as a function of
  volume/liquidity/platform/horizon, isotonic-repair on/off, dedup strategy —
  currently hand-set priors — get tuned against held-out gym loss instead of
  argued about.

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
5. **Forecast-skill packaging**: extend `skills/superforecaster/` vs a sibling
   skill that wraps loom's dated data tools + authoring/fit API and defers to
   superforecaster for elicitation methodology. Default: sibling skill,
   cross-referenced.

## Risks

- **Coverage/ESS collapse** — the spike's recurring failure mode. Mitigation:
  hard gates in every fit + fit-don't-reweight for anything sparse.
- **Correlated/near-duplicate markets** double-counting one latent. Mitigation:
  dedup/cluster weighting in M1; genuinely open research — watch the residuals.
- **Scope creep into rebuilding augur.** Mitigation: the dependency rule;
  anything deterministic or holdings-specific belongs to augur/gaffer-private.
- **Quiet divergence of duplicated code.** Mitigation: never copy from augur —
  migrate atomically or keep depending the allowed direction.
