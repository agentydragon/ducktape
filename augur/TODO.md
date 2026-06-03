# Augur TODO

Public, generic Augur backlog. Sim-specific follow-ups live in
`augur/sim/TODO.md`; downstream repos keep private composition,
deployment, and user-/company-specific modeling assumptions in their own
trackers. Priority ordering lives in `augur/plans/roadmap.md` — keep
this file as a backlog, not a second ordered roadmap.

## Eliminate magic-prefix series/asset strings (staged)

Roadmap + design: <augur/plans/typed_series_config.md>. Goal: no `crypto:` /
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
    `latest_observations`. Investigation found these are keyed by trained-blob
    factor wire-ids (and the vecm blob is a heterogeneous data-provenance map
    keyed by source names like `spy_adjusted_close_latest`, with wire-ids only as
    inner `*_by_factor` keys), and `fit/state_space.py` injects PE-issuer
    observation keys too. Retyping them coherently means typing the factor
    identity in the artifacts first — so they move to Phase 3 alongside the
    `StateSpaceModelArtifact` / vecm `.npz` factor retype.
- [ ] Phase 2 — polars frames `series_id`/`asset_id`/`event_id` → per-kind frames
      (no kind column; sub-id only) + sim compiler/codec/projections/decode sweep
- [ ] Phase 3 — typed artifact factors: state_space JSON + vecm `.npz`; regenerate
      blobs (incl. augur OCI image layer). Then conditioning `observations`,
      `location_series_sources`, vecm `latest_observations` (deferred from Phase 1)
      retype against the now-typed factor identity.
- [ ] Phase 4 — API wire (`asset_id`, `spend_index`) typed; delete `wire_id`/`parse_*`;
      add CI prefix-guard

## Whole-model calibration

Lift calibration from one PE issuer ("a stock") to the whole augur joint model,
scoring prediction markets across macro channels too (S&P, inflation, …) as
marginals. Design + gotchas: <augur/plans/whole_model_calibration.md>. v0
measures per-market only — no aggregate loss, no fitting.

- [ ] **Slice 1 — macro markets in the calibration view.**
  - [x] Vectorized macro resolvers over the `(rollout, month)` channel matrix
        (no per-rollout object): `level_at_date` (point-in-time threshold),
        `inflation_yoy` (12-month change), both returning `ResolutionCounts`,
        with unit tests. Keep `RolloutTrajectory` for the PE path.
  - [x] Multinomial bucket families: `BucketFamily` catalog type + categorical
        `D_KL(market ‖ model)` scoring (per-bucket p_market/p_model), with tests.
  - [x] Anchor macro series to live spot at `as_of` (catalog `anchors`).
  - [x] Thread macro scoring through `run_calibration` / API `/api/calibration/run`
        / `calibration_report` CLI: sample the macro level series the catalog
        needs, intersect with `emittable_level_keys` (surface unmodeled).
  - [x] Seed the example catalog with the initial S&P (Manifold + Kalshi bucket
        family) and CPI (Kalshi) markets.
  - [ ] Frontend: render categorical bucket families; macro Bernoulli markets
        flow through the existing clean table.
- [ ] **Typed `MarketMapping`.** Replace loose `mapping_kind: str` +
      `mapping_params: dict` with a discriminated union (issuer-event / level /
      inflation); make invalid bindings unrepresentable. Atomic catalog rewrite.
- [ ] **Drop `CalibrationCatalogConfig.issuer`.** Catalog markets self-describe
      their target; the run covers the union of referenced issuers/series.
      Atomically switch gaffer's config to the new shape.
- [ ] **Macro level fans** in the calibration view via a generalized `level_fan`
      (today's `mark_fan` is PE-only).
- [ ] **Aggregate metric (later, weighting TBD).** Per-channel / volume-weighted
      rollups of per-market KL once the weighting policy is decided.

## Exogenous Models & Evidence

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
  - [ ] **M2.2-A — per-rollout stochastic dilution rate (sampler + fit landed).**
        Each rollout draws `r = annual_dilution_rate · exp(annual_dilution_rate_log_sigma · z)`,
        `z ~ N(0,1)` (median-anchored LogNormal), off its own `:pe_risk_dilution` seed stream
        via ONE unified path — σ=0 (default) degenerates naturally to the M2 deterministic
        factor, byte-identical, no special-case. New config `annual_dilution_rate_log_sigma`.
        Fit landed too: `augur/fit/dilution_prior.py` + `derive_dilution_prior` binary
        (implied-shares = valuation/price log-linear OLS → rate + honestly-wide σ_r).
        Independent of `V(t)` for now (conservatively over-states per-share spread; the hedge
        is M2.2-B). No new bundle event kind. **Follow-up — BLOCKED on M2.2-D:** wiring
        `derive_dilution_prior` on the gaffer evidence into `config.yaml` was attempted
        (gaffer `claude/magical-cannon-USFo4`, committed as a NOT-deployable record) and
        FAILS the `sample_sanity` gate — the OLS fit's ~244%/yr valuation drift compounds to
        an ~8000× median 10y mark. Deployment waits on M2.2-D (NUTS + decaying drift).
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

- [ ] **Replace the disjoint buffer policies with a rebalance-to-target
      model.** Today's product surfaces two policies that look
      superficially similar but live in different shapes and don't
      compose: - `FundingPolicy` (in `augur/product/wire.py`) — cash buffer: when
      post-obligation cash drops below a dollar trigger, sell a fixed
      dollar amount from `public_securities`. Trigger + sell-amount
      come from the frontend per scenario. - `PrivateEquityTenderPolicy` — liquid-net-worth floor: only sell PE
      through a tender if liquid_net_worth ≥ floor. Floor comes from
      the frontend per scenario.

      A cleaner unifying frame is **rebalance toward a multi-tier target
      allocation**: each tier (cash, liquid public stock, PE, possibly
      property) has a target dollar amount (or fraction of net worth), and
      the policy nudges holdings toward those targets each month by
      buying/selling between tiers. A deadband around each target avoids
      churn. This shape captures everything the existing floor-based
      policies do (a "floor" is a one-sided target with infinite upper
      deadband) plus three things the existing policies can't:
      - the missing middle tier (a stock-value target that triggers PE
        sales when stock holdings fall below it);
      - reinvestment of PE sale proceeds (proceeds from a tender flow
        toward whichever tier is below its target, not flat to cash);
      - upside rebalancing (if PE grows to dominate net worth, sell into
        tenders even without a cash need, to refill the stock tier).

      Frontend exposes target amounts and deadbands per tier; the
      simulator runs the rebalance rule each month against available
      sale capacity (PE tender opportunities, public-stock liquidity).
      The existing `FundingPolicy` + `PrivateEquityTenderPolicy` shapes
      become special cases of this surface and can be deprecated.

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
