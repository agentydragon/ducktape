# Augur TODO

Public, generic Augur backlog. Sim-specific follow-ups live in
`augur/sim/TODO.md`; downstream repos keep private composition,
deployment, and user-/company-specific modeling assumptions in their own
trackers. Priority ordering lives in `augur/plans/roadmap.md` — keep
this file as a backlog, not a second ordered roadmap.

## Eliminate magic-prefix series/asset strings (staged)

Roadmap + design: <augur/plans/typed_series_config.md>. Goal: no `security:` /
`home_value:` / `rent:` / `private_equity:` prefix strings anywhere — kind
carried structurally (typed config unions; per-kind frames carrying only a
sub-id column; typed artifact factors). Staged; each phase lands green on its
own. **Not done** until `wire_id` / `parse_level_series_key` / `parse_asset_key`
are deleted and the CI prefix-guard is in place.

- [ ] Phase 1 — config typed. Clean config surfaces landed:
  - [x] `LevelSeriesKind` / `AssetKind` → `StrEnum`
  - [x] `LevelSeriesGroups[ValueT]` per-kind helper
  - [x] `IndependentExogenousProviderConfig` (provider) typed
  - [x] `series_model.py` `IndependentSeriesModels` (sim/bench twin)
  - [x] portfolio `value_series` (`HoldingPositionConfig`)
  - [x] `sample_sanity` checks (`key: LevelSeriesKey`, typed `required_*`)
  - [ ] gaffer migration (repin + YAML + config_test discrepancy)
  - **Deferred to Phase 3** (not clean config — entangled with trained-artifact
    factor names): conditioning `observations`, `location_series_sources`, vecm
    `latest_observations`. Investigation found these are keyed by trained-artifact
    factor wire-ids (and the vecm `trained_state` sits beside a heterogeneous
    data-provenance map keyed by source names like `spy_adjusted_close_latest`,
    with wire-ids only as inner `*_by_factor` keys), and `fit/state_space.py`
    injects PE-issuer observation keys too. Retyping them coherently means typing
    the factor identity in the artifacts first — so they move to Phase 3 alongside
    the `StateSpaceModelArtifact` / vecm `trained_state` factor retype.
- [ ] Phase 2 — polars frames `series_id`/`asset_id`/`event_id` → per-kind frames
      (no kind column; sub-id only) + sim compiler/codec/projections/decode sweep
- [ ] Phase 3 — typed artifact factors: state_space JSON + vecm `trained_state`;
      regenerate artifacts (state_space's OCI image layer included — vecm no
      longer has one). Then conditioning `observations`, `location_series_sources`,
      vecm `latest_observations` (deferred from Phase 1) retype against the
      now-typed factor identity.
- [ ] Phase 4 — API wire (`asset_id`, `spend_index`) typed; delete `wire_id`/`parse_*`;
      add CI prefix-guard

## Whole-model calibration

Calibration scores prediction markets across the whole augur joint model (per-issuer
PE events + macro channels like S&P / inflation) as marginals, per market. Design +
gotchas: <augur/plans/whole_model_calibration.md>. Landed: typed `MarketMapping`,
vectorized macro/inflation resolvers, multinomial bucket families, anchoring, the
issuer-agnostic run (catalog self-describes issuers/series), the frontend, and
git-mirrored market snapshots in augur-evidence (the scraper mirrors every
catalog-referenced market; the valkey read-through cache + live per-platform
clients were deleted in favor of checkout reads).
Remaining:

- [ ] **Surface market-quote fetched-at in the UI.** The scrape manifest
      (`evidence_meta.json`) records each market's last successful sync; plumb it
      through so the calibration page can show quote age (today staleness is only
      bounded by the scraper cadence, invisibly).
- [ ] **Macro level fans** in the calibration view via a generalized `level_fan`
      (today's `mark_fan` is PE-only).
- [ ] **Prediction-market quote quality.** _Mostly landed:_ `Market` now carries a structured
      `quote` (`calibration/quote.py`: `BookQuote` bid/ask/size/last, `PoolQuote` for AMMs);
      `implied_probability` uses the Stoikov micro-price (mid-else-vol-backed-last-else-None, no
      fake `0`/`0.5`); ladders aggregate via confidence-weighted isotonic regression + discrete
      Breeden–Litzenberger differencing (`calibration._fit_ladder_curve`). RCA + before/after in
      `augur/debug/cpi_yoy_market_line_spikiness.md`. **Remaining:**
  - [ ] **Surface quote quality in the UI.** Show stale / wide-spread / one-sided / no-quote
        states (and per-rung confidence) in `frontend/calibration.tsx`; today low-confidence rungs
        are silently down-weighted in the fit but the user can't see which.
  - [ ] **Polymarket depth for micro-price.** The gamma response carries best bid/ask but no size,
        so Polymarket degenerates to the plain midpoint; fetch the CLOB order book for true depth.
  - [x] **Honest naming.** `Market.probability` / `require_probability` renamed to
        `Market.quote` / `require_implied_probability`; markets expose quotes, not probabilities.
- [ ] **Aggregate metric (later, weighting TBD).** Per-channel / volume-weighted
      rollups of per-market KL once the weighting policy is decided.

## Exogenous Models & Evidence

- [ ] **Drop the redundant "exogenous" adjective from remaining identifiers, docs,
      and comments.** Every augur model is exogenous by invariant, so the adjective
      carries no information. Model labels, config descriptions, and several
      docstrings were already cleaned; what remains is the high-blast-radius type
      renames plus the long tail of docstrings/comments/docs:
  - Type renames across `model/`, `sim/`, `product/`, `calibration/`:
    `SampledExogenousBundle` → `SampledBundle` (`augur/model/exogenous.py`),
    `materialize_sampled_exogenous` → `materialize_sampled_bundle`
    (`augur/sim/external_series.py`), `ExogenousPathId` / `ExogenousPathSet`,
    plus all imports/usages and the `exogenous_path_id` API field
    (`augur/api/accounting.py`).
  - Module/file docstrings in `augur/model/*`, `augur/fit/*`,
    `augur/calibration/*` that still say "exogenous model/provider/series".
  - Internal comments and error messages (`augur/model/exogenous.py`,
    `augur/sim/external_series.py`, `augur/product/{service,scenarios}.py`).
  - Documentation files: `augur/SPEC.md`, `augur/README.md`,
    `augur/model/MODEL_CARD.md`, `augur/sim/DESIGN.md`, `augur/docs/prior_art_audit.md`,
    `augur/plans/{roadmap,typed_series_config}.md`, and the gaffer-private mirror.
- [ ] **Split augur-evidence fetch cadence by source freshness.** The in-cluster
      scrape + git-sync checkout is live; what remains is reducing unnecessary
      upstream fetches. Keep daily jobs for price/market sources that can move
      daily, and gate or split slower FRED/Zillow/FHFA/rent sources by their
      weekly/monthly/quarterly publication cadence. Git already no-ops unchanged
      bytes, so this is about upstream load and clearer freshness expectations.
- [ ] **Add an in-cluster refit/publish job for trained evidence artifacts.**
      Fresh scraped evidence updates checkout reads, but state-space
      `latest_observations` and trained artifacts still require a gated
      `fit:train` / `sample_sanity` refresh. Run the fit in cluster, fail closed
      on the gate, and publish the artifact/config update for review across the
      public/private repo boundary.
- [ ] **Plaid-fed portfolio granularity.** v0 Plaid portfolio import maps configured
      public-stock/ETF holdings into synthetic SP500 proxy lots. Later, add a
      per-security import mode with explicit model-series mapping for supported tickers,
      crypto, and non-SP500 equity proxies instead of collapsing all public equities into
      one broad factor.
- [ ] **Plaid-fed tax-lot reconstruction.** v0 Plaid portfolio import creates one
      synthetic aggregate lot per imported position using current holding value and
      aggregate cost basis when Plaid provides it. Later, reconstruct more granular lots
      from investment transactions or a tax-authoritative brokerage export, with clear
      treatment for incomplete history, transfers, and missing acquisition dates.
- [ ] **VECM NumPyro h=1 fit-quality vs statsmodels baseline.** After
      the NumPyro migration (log-scale Cholesky, ridge-regularised α / β,
      20k SVI iters), held-out per-month log-density is +15.85 nats on
      the testdata evidence vs the statsmodels MLE baseline of +17.79 —
      about 2 nats off at h=1. Multi-step h=12 is actually _better_
      (+0.88 vs -5.50; the ridge penalty curbs spurious mean reversion).
      The 2-nat gap at h=1 is most likely the priors' shrinkage on α / β
      biasing the cointegration pull on training-window residuals.
      Options to close it: (1) further-tighten or anneal the priors over
      iterations; (2) preconditioned SVI / second-order MAP for tighter
      convergence; (3) NUTS for the full posterior, dropping point
      estimates entirely. Stretch goal: held-out per_month within ~1 nat
      of +17.79.
- [ ] **Independent provider in metric_report.** The testdata YAML uses
      synthetic location ids (`home_value:location_a`, `rent:location_a`)
      that don't match the historical evidence's factor names
      (`home_value:san_francisco_ca`, etc.). `IndependentExogenousModel.predictive`
      returns None when any requested factor is missing, so the metric
      report emits Unscored rows for Independent. Either (a) build a
      synthetic Independent provider from the historical's factor list
      and empirical per-factor mean/std, or (b) seed the metric report
      from a different YAML config that aligns with the evidence.
- [ ] **Per-location rent series.** The VECM fits a single
      `rent:san_francisco_ca` factor (FRED SF rent CPI) and aliases it
      for every other location via `location_series_sources.rent`. Today
      `rent:vallejo_ca` and `rent:mare_island_vallejo_ca` are literally
      the same trajectory as SF rent. Wire dedicated FRED rent CPI
      series (or equivalent) per location, fit them as additional VECM
      factors, and remove the SF-rent aliasing.
- [ ] **Mare Island home_value distinct from Vallejo.** Today
      `home_value:mare_island_vallejo_ca` aliases to the Vallejo Zillow
      ZHVI factor (gaffer config piggybacks). Source separate Mare
      Island data — Zillow may not stratify it, so this likely needs a
      different provider or a deliberate adjustment factor.
- [ ] **Document Zillow city discovery.** The Zillow CSV already has
      every US metro; the loader filters to the configured
      `zillow_home_value_regions` set. Adding a city to a deployment
      should just be a config edit, but the "available cities" surface
      should be documented with a discovery helper.
- [ ] **Probabilistic mortgage-offer model, conditioned on term.** Today
      scenarios take a single flat `financing.mortgage_rate_pct` from
      user input. The "what rate would the buyer actually get at
      purchase month?" question deserves its own model — output a
      distribution over offered rates conditioned on term (30y fixed,
      15y fixed, jumbo, ARM, …), optionally on credit score / LTV /
      loan size, so scenario builders can sample a realistic offer
      instead of hard-coding one. FRED has term-stratified series;
      could fit jointly with the broader rate factor once the consumer
      side is wired (see Next Lanes "Mortgage-rate path sampling" in
      `augur/plans/roadmap.md`).
- [ ] Consider whether the deleted legacy exogenous models should be
      revived as runtime-native `augur/model` implementations where they
      are actually useful: `var` as a lighter joint macro baseline,
      `dcc_garch` for time-varying liquid-market/crypto volatility and
      correlations, `wilkie` for actuarial long-horizon
      inflation/equity/rate scenarios, and `bootstrap` for empirical
      stress/history resampling. Do not revive the old
      `macro_market_bundle_provider` shape directly; mine it only for
      data loading/config ideas after the native `SampledExogenousBundle`
      API is the target.
- **Stochastic dilution + evidence fit (M2.2).** M2
  (`augur/plans/prediction_market_calibration.md`) landed the coupled, opt-in
  company-valuation channel —
  `latent_mark = current_mark × (V(t)/V₀) / dilution_factor(t)` with `V(t)` a Student-t
  random walk — but `dilution_factor(t) = (1 + annual_dilution_rate)^(t/12)` is
  **deterministic** (identical in every rollout → zero per-share spread; all holding-value
  uncertainty currently comes from `V(t)`), and `shares_outstanding_initial` /
  `annual_dilution_rate` / the valuation RW params are hand-set, not evidence-fit. The
  trainer still collapses `valuation_observation`s to a static
  `scale_prior.current_market_cap_usd`. Design + rationale (structure vs fit, identifiability,
  the primary/secondary + stale-FMV likelihood caveats) is the **M2.2 section** of
  `augur/plans/prediction_market_calibration.md`. Staged:
  - [x] **M2.2-A — per-rollout stochastic dilution rate (sampler landed).**
        Each rollout draws `r = annual_dilution_rate · exp(annual_dilution_rate_log_sigma · z)`,
        `z ~ N(0,1)` (median-anchored LogNormal), off its own `:pe_risk_dilution` seed stream
        via ONE unified path — σ=0 (default) degenerates naturally to the M2 deterministic
        factor, byte-identical, no special-case. New config `annual_dilution_rate_log_sigma`.
        Independent of `V(t)` for now (conservatively over-states per-share spread; the hedge
        is M2.2-B). No new bundle event kind. The superseded implied-shares OLS prototype
        failed the `sample_sanity` gate — its ~244%/yr valuation drift compounded to an
        ~8000× median 10y mark — and was retired after the M2.2-D Bayesian fit replaced it.
  - [ ] **M2.2-B — V↔dilution correlation.** Joint per-rollout `(V log-drift, r)` draw with
        fitted ρ (good-company worlds dilute more → per-share partly hedged; independence
        over-states per-share spread). Makes `V(t)`'s drift per-rollout — a change to the V
        sampler, hence its own phase.
  - [ ] **M2.2-C — lumpiness + secondary rigor (deferred).** Discrete primary-round dilution
        as a new V-coupled event kind, distinct from the existing (secondary) tender events;
        likelihood that attributes only primary rounds to share growth and treats secondaries
        (e.g. the 2025-10 employee tender) as pure re-pricing.
  - [ ] **M2.2-D — Bayesian fit + scale-dependent mean-reverting drift (critical path to
        deploying the openai fit; full design in the M2.2-D section of
        `plans/prediction_market_calibration.md`).** - [x] **NUTS fit** `augur/fit/bayes_dilution.py` — numpyro state-space model (latent log-V
        RW + log-share path, per-obs `uncertainty_log_sigma` likelihood, informative priors)
        over all obs, mirroring `augur/model/vecm.py`. Sane dilution (r≈0.31, σ≈0.05) + honest
        posterior σ. Regularizes the OLS over-extrapolation (8035× → 381× median 10y mark). - [x] **calendar-decay drift** in the sampler (`valuation_monthly_log_return_mu_initial` /
        `valuation_drift_decay_halflife_months`). Backward-compatible (off ⇒ byte-identical). - [x] **scale-dependent mean-reverting drift (load-bearing).** Replaced calendar decay with
        `mu_V(s) = mu_mature + (mu_young−mu_mature)·exp(−max(0,s−s_young)/s_scale)`, `s=log V` —
        drift high when small, reverts toward mature as the company grows (self-correcting per
        rollout). `V(t)` is now an SDE integrated per-month (vectorized across rollouts) in both
        the sampler and the `bayes_dilution` fit. **Finding:** a single issuer whose data is all
        in one size regime CANNOT identify the shape (the full fit diverges — see the M2.2-D
        KEY FINDING in `plans/prediction_market_calibration.md`), so `fit_bayesian_dilution_prior`
        gained `fit_scale_reversion_shape` (default False): fix the shape at the
        `BayesianDilutionPriors` centers (each justified in that dataclass's per-field docs) and
        fit only the identifiable params (level, σ_v, shares, dilution). The forward `σ_v` prior
        is anchored at the de-smoothed late-stage-VC figure (~38%/yr), NOT the boom-era in-sample
        scatter (~73%/yr), for the same forward-vs-in-sample reason as the drift. Validated
        end-to-end on the openai evidence: stable, and inside all four `sample_sanity` mark bands.
        Fitting the shape itself is deferred to the population prior (below). - [ ] **deploy mechanism (orthogonal, later).** Scalar-summary config knobs now; full
        posterior-predictive deploy (private gaffer trained artifact, like
        `state_space_macro_artifact.json`) as a later uncertainty-propagation upgrade. Does not
        fix the median — follows the scale-reversion work.
  - [ ] **Realization-loss tail toward population base rates (loss-tail counterpart to M2.2-D
        drift; same model-fix philosophy).** Going-concern drift asymptoting to ~market return
        does NOT model "lose everything" — that lives in the collapse / legal-impairment /
        forced-recovery hazards + no-liquidity tail. Population base rates show augur currently
        UNDER-states it: 2010–15 Series C → only 38% exited in a decade (62% no liquidity); post-
        2021 >80% of unicorns below peak; WeWork −99.9%. Same gap as the measured pre-IPO-failure
        calibration miss (model 0.014 vs market 0.092). Bump the collapse/no-liquidity hazards
        toward these rates; ultimately fit them in the same hierarchical population prior.
  - [ ] **Hierarchical population prior (north star — the real reason for the Bayesian
        machinery).** Fit the scale-reversion hyperparameters + dilution + loss-hazard base rates
        across a POPULATION of startup trajectories, treating each issuer as a partially-pooled
        draw — converts every educated guess above into a pooled estimate and calibrates the loss
        tail at once. **Survivorship bias is the central trap** (cheap datasets over-represent
        survivors ⇒ drift biased up, loss tail down — backwards for realization risk); the panel
        MUST include down rounds, shutdowns, never-exited "walking dead." Reserve evidence-schema
        room for terminal/failure outcomes. Also the eventual home of M2.2-C primary/secondary
        round labels + sizes.

## Liquidity Policy

The cash side of this is **done**: `FundingPolicy` is an (s,S) cash band plus
per-holding target weights, lowering to `TargetAllocationPolicy`, and sales are
water-filled against the target rather than taken from an ordered sell list.
What remains is composing that target with the PE tender floor, and buying.

- [ ] **Bring private equity into the same target.** `PrivateEquityTenderPolicy`
      is still a separate shape: a liquid-net-worth floor gating tender sales,
      with its own frontend knob. It does not compose with `sleeve_weights` —
      a floor is a one-sided target with an infinite upper deadband, so it
      should BE a sleeve, with the tender's sale capacity as the constraint on
      how fast that sleeve can be drained. Blocked on nothing in particular;
      wants the PE lot axis to be reachable from `ActorView`.

- [ ] **A ladder that rolls.** Today an inflation-indexed ladder can only be HELD from
      scenario start (`BondHolding`), never extended. Since TIPS are issued in 5/10/30-year
      terms only, ~30 years is the longest real floor that can be contracted for at all, so any
      horizon past that needs rungs bought mid-simulation at the real yield prevailing then.
      Absent that, the simulator never samples a bad roll and is biased toward shallow ladders —
      it cannot price the "defer and buy later" strategy at all, only assume it works.
      Three pieces:
  - **A curve that reaches past 10 years — do this first, it is the smallest piece and it
    unblocks the other two.** `_instrument_yield` clamps at `min(duration/10, 1)`, so a 30-year
    bond is priced at the 10-year yield and most of a real ladder is invisible to the model
    (SPEC gap 8 sizes the error). A Gaussian VAR admits an affine term structure whose loadings
    are compile-time constants, so the whole curve is a matmul against the `(R, H, 3)` state
    path already emitted — no new series, no new stochastic dimensions. The real curve is the
    same recursion with `r − π` as the short rate. This is also what bond mark-to-market needs
    (`sim/TODO.md` § Bonds), so three backlog items share one mechanism.
  - Mid-horizon purchase of an indexed bond. The blocker once believed to exist — that a rung
    bought in month `t` delivers month-`t` dollars, unknowable at config time — is not real: a
    TIPS pays `face × CPI_T/CPI_t`, so delivering `X` month-0 dollars needs
    `face = X × CPI_t/CPI_0`, known at PURCHASE time. The genuine new machinery is that
    `annual_coupon_rate` becomes traced (it is the prevailing real yield) instead of a
    compile-time constant, moving bond cashflows off the static table — the path
    `inflation_indexed=True` already opened.
  - The trigger is a POLICY, not a schedule. An unconditional scheduled buy is not a neutral
    default, it is the maximally-forced-buyer strategy, and its underfunding clamp is silent —
    realized ladder depth would become path-dependent and unreported, i.e. measuring a strategy
    with no name. Mid-horizon acquisition now uses the target-allocation policy's purchase
    slots, so the policy maintains K years of real
    coverage, with a real-yield threshold knob below which it defers; that knob is what turns
    "wait and decide later" from prose into a measurable arm. A schedule stays useful as a
    deterministic test fixture for the execution layer, not as a second config path.
    Open design questions: whether the ladder joins the target-allocation denominator (today it
    cannot — sleeves outside the denominator are what make a target alongside an untradeable
    holding expressible); phase ordering against the cash band, since both want the same dollars
    in the same month and getting it wrong manufactures churn; and that a coverage target the
    policy cannot afford must be a reported event, never a clamp.

- [ ] **Trim a sleeve that has grown to dominate.** The buy side is done:
      a policy with `purchase_slots_per_sleeve > 0` invests cash above the
      ceiling down to the floor, water-filled into whichever sleeves are
      furthest below target. What is still missing is the other direction —
      selling an overweight sleeve when there is no cash need at all. Today a
      sale only ever happens to fund something, so a sleeve that doubles is
      never trimmed. Doing it needs a drift tolerance, and a tolerance needs
      the tax drag it causes to be measurable, which is what the allocation
      study is for.

## API / Runtime Design Debt

- [ ] **Extend policy schema/programs** enough for downstream
      deployments to express concentrated-holding limits,
      liquidity-sale preferences, tender/acquisition/IPO preferences,
      and tax preferences without ad-hoc `Config` fields.
- [ ] **Separate rollout stochastic inputs in the API/data model.**
      Today a rollout is effectively a sampled exogenous trajectory
      plus deterministic policy. Make that explicit, and consider
      separate structures/identifiers for exogenous-path
      nondeterminism, policy nondeterminism, and any future non-policy
      random events so trajectory IDs don't conflate different sources
      of randomness.
- [ ] **Clarify initial state vs scheduled transitions.** Property
      purchase, financing, ownership, future sale, rental transition,
      and private-stock sale opportunities should not be split across
      fields/events that can contradict each other.
- [ ] **Reduce single-property/global assumptions.** Scenario-level
      `property_selection`, `financing`, `rental_plan`, and
      `tax_profile` should eventually become initial positions,
      per-property settings, or per-actor/accounting inputs as the
      simulator grows.

## Reporting / UI

- [ ] **Reframe the Augur UI around the user's conceptual model**
      instead of the simulator's current implementation seams. The
      sidebar mixes initial holdings, policy knobs, exogenous market
      events, tax constants, financing assumptions, actor
      participation, and result summaries in ways that make the product
      feel internally confused. Start with an information architecture
      pass: define scenario identity, actors/ownership, initial balance
      sheet, market assumptions, policy choices, tax assumptions, and
      output diagnostics as separate concepts before continuing local
      control tweaks.
- [ ] **Comparison views for deltas between two scenario
      distributions.** Either the distribution of differences between
      samples from both distributions, or paired differences
      conditioned on the same underlying exogenous path. Complements
      the multi-scenario comparison item in `augur/sim/TODO.md`.
- [ ] **Shared browser visual-test utilities** for deterministic
      Playwright runs. Augur visual goldens currently carry their own
      Chromium determinism flags and injected determinism CSS; those
      should move into a shared repo helper alongside similar browser
      visual tests so each app doesn't invent its own deterministic
      screenshot harness.
- [ ] **Browser/UI for PE protocol controls and inspection.** The
      model/sim protocol now distinguishes private operating, public
      market, acquired, and collapsed regimes plus tender/admin/legal/
      forced-recovery event kinds. The browser still only exposes the
      single fixture PE holding and tender floor shape — no UI for
      configuring public-market lockup, acquisition cashout, legal
      block, recovery, or collapsed-path assumptions.
- [ ] **Normalize result table labels.** Headers like `P50 net worth`
      next to `liquid worth` are inconsistent because the second value
      is also a percentile, and "liquid worth" is unclear wording.
      Prefer explicit, consistent labels such as `P50 liquid net worth`
      or whatever term the model settles on.
- [ ] **Hover-driven trajectory inspection on distribution charts.**
      The fan chart should keep showing distribution envelopes and the
      central median/mean line by default, but hovering should reveal
      the selected rollout trajectory line without permanently
      rendering every rollout. While hovered, distribution-page summary
      numbers should switch where meaningful from aggregate values to
      that rollout's values, and time-sensitive values should use the
      hovered x-coordinate/month. The hover detail should also show
      the hovered trajectory's percentile at that point and, if
      practical, lightly highlight the complementary
      percentile trajectory/range.
- [ ] **Richer distribution fan rendering.** The current chart shows
      one envelope range plus one middle line, but the earlier private
      prototype had a fuller continuous-color fan showing more of the
      distribution. Consider bringing that back after the
      distribution/trajectory structure settles.
- [ ] **Reorganize tax controls** so capital-gains rates, exclusions,
      and other tax constants live together and apply consistently to
      stock, private-equity, and property-sale gains. Verify the
      current math before moving controls.
