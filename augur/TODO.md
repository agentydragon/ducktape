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

- [ ] Phase 1 — config typed. Foundations + first surface landed:
  - [x] `LevelSeriesKind` / `AssetKind` → `StrEnum`
  - [x] `LevelSeriesGroups[ValueT]` per-kind helper
  - [x] `IndependentExogenousProviderConfig` (provider) typed
  - [x] `series_model.py` `IndependentSeriesModels` (sim/bench twin)
  - [ ] portfolio `value_series`
  - [ ] conditioning, location_series_sources, vecm latest_observations
  - [ ] sample_sanity checks
  - [ ] gaffer migration (repin + YAML + config_test discrepancy)
- [ ] Phase 2 — polars frames `series_id`/`asset_id`/`event_id` → per-kind frames
      (no kind column; sub-id only) + sim compiler/codec/projections/decode sweep
- [ ] Phase 3 — state_space JSON + vecm `.npz` typed factors; regenerate blobs
      (incl. augur OCI image layer)
- [ ] Phase 4 — API wire (`asset_id`, `spend_index`) typed; delete `wire_id`/`parse_*`;
      add CI prefix-guard

## Exogenous Models & Evidence

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
- [ ] **Fit the PE valuation + share-dilution process, not just a static
      scale.** M2 (`augur/plans/prediction_market_calibration.md`) adds a
      company-valuation channel `V = mark × shares(t)` plus a dilution
      process, but v1 uses point estimates (`shares₀`, `annual_dilution_rate`).
      The trainer already ingests `valuation_observation`s but collapses them to
      a single static `scale_prior.current_market_cap_usd`; make the scale
      **dynamic and Bayesian** — fit a posterior over the dilution rate and the
      valuation drift/vol jointly from the paired (price, valuation) series so
      `shares(t)` / `V(t)` carry real uncertainty instead of hand-set points.
      Distinguish primary-issuance dilution (funding rounds) from secondaries
      (no new shares) and employee-equity (PPU/RSU) issuance.

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
