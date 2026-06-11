# Augur TODO

This file tracks public, generic Augur backlog. Downstream repos keep private
composition, deployment, and user-/company-specific modeling assumptions in
their own trackers.

Priority ordering lives in `plans/roadmap.md`. Keep this file as the public
generic backlog rather than a second ordered roadmap.

## Next

- [ ] **Finish the sim-native API contract now that backend execution is
      sim-only.** Track the concrete replacement work in `augur/sim/TODO.md`.
      The production path now translates current API payloads, expands scalar
      seeds into explicit per-rollout seeds, samples a shared
      `SampledMarketBundle`, evaluates with `augur/sim`, and derives frontend
      graph tables from sim dataframes. Remaining work is to broaden the
      translator/materialized response beyond the current smoke slices and
      replace the temporary compatibility response with native projection read
      models. The next translator slice should move opening public securities
      into backend YAML config as actual units, starting prices, and cost basis,
      with scenarios referencing those configured assets rather than carrying
      editable `value_usd` buckets. The migration inventory for current API
      fields lives in `augur/sim/TODO.md#api-to-sim-translation-inventory`.
- [ ] **PE valuation should actually be sampled** (Priority 3 in
      `plans/roadmap.md`). The market provider holds private-equity marks
      flat at 1.0 for the entire horizon. The fit is **open design work**
      — available evidence is sparse (e.g. ~5-10 historical OpenAI
      tenders), so the natural shape is a model fit **jointly** with
      SP500, inflation, and the per-location housing factors that the
      macro provider already estimates (VECM/VAR/Wilkie/etc.), rather
      than an independent process per PE asset.
- [ ] **Tender timing should be sampled**, fitted jointly with the PE
      price model on the same sparse evidence. Today the market provider
      emits tender opportunities at deterministic month indices (every 12
      months from t=0, identical across rollouts and PE assets). The
      explicit `PrivateEquityLot.tender_windows` path lands deterministic
      windows from portfolio statements; this followup adds a stochastic
      fallback for the open-ended horizon.
- [ ] **Crypto price should be sampled.** Runtime asset class +
      funding-policy wiring landed via #1582; the remaining gap is
      replacing the `np.ones(...)` placeholder `crypto_value_multipliers`
      array with a sampled per-asset path so crypto contributes real
      variance.
- [ ] **Mortgage rate should be sampled.** The macro provider holds the
      30y rate flat at today's value over the entire horizon. Refinance
      and variable-rate scenarios unreachable until the joint market
      model produces a sampled mortgage-rate path.
- [ ] **`RegimeChange` mid-rollout events.** The `LiquidityRegime` shape
      already supports `LiquidityEventOnly → PublicMarket` transitions
      (e.g. IPO), but no runtime hook flips the regime today. Markets
      should be able to sample a regime change at a future month.
- [ ] Make the generic Augur OCI image public-safe: no private Python
      config, property records, or media in image layers; deployments
      supply private config and assets through mounted runtime inputs.
- [ ] Add a durable property-asset storage contract: stable property
      asset IDs/URLs backed by object storage or a database-like asset
      table, so deployments do not need to bake private media into
      frontend images.
- [ ] Index `OccupancyPlan.outside_rent_monthly_usd` to modeled rent costs.
      Today the monthly amount is flat; a real horizon should be configured as
      "rent is `$X` on the scenario/config as-of date, then moves linearly with
      this `augur/model` rent-cost series for this rental market." In sim terms,
      future rent should be derived from the configured base rent multiplied by
      `rent_cost_series[t] / rent_cost_series[as_of]`. CPI or wage indexing can
      be model choices behind that series, not hard-coded sim behavior.
- [ ] Apply the same rent-cost indexing contract to tenant rent income for
      owned properties. Once sim has native rental cashflows, configured tenant
      rent should start from today's rent and scale by the modeled rent-cost
      series for the property's rental market, instead of staying flat or using
      hard-coded CPI/wage growth.
- [ ] Wire the sim's shared path-indexed amount contract through backend/API
      translation for model-driven cashflows. The sim primitive now supports
      base amount plus model series key plus fixed reset period; the remaining
      product work is using it from YAML/API config for outside rent, tenant
      rent, inflation-adjusted spend, and later recurring property costs.
- [ ] Drive owned-property mark-to-market values from configured purchase/list
      value plus modeled home-value series. For the first sim backend cutover,
      assert property purchase happens at month 0; defer future purchase-price
      semantics until there is a real product case.
- [ ] Keep the first sim backend property lifecycle slice deliberately narrow:
      no mid-horizon purchase, no rental start/stop transition, occupancy is
      forever when selected, and sale can be end-of-horizon only if needed for
      graphable outputs.
- [ ] Split economic counterparties and accounts in sim scenarios. Landlord,
      tenant, lender, seller, tax authority, HOA, insurer, brokerage, and crypto
      exchange identities should be explicit enough for accounting/reporting,
      with escrow omitted until a real workflow requires it.
- [ ] Treat backend and sim amounts as nominal dollars for now. If real-dollar
      views are needed later, add them as response/read-model postprocessing
      rather than alternate simulator accounting.
- [ ] Make YAML deployment config the source of truth for initial positions and
      other facts that are not product knobs. Bootstrap/UI defaults should
      derive from config, and unsupported scenario toggles can be removed or
      hidden while the sim cutover is narrowed.
- [ ] Consider whether the deleted legacy market models should be revived as
      sim-native `augur/model` implementations where they are actually useful:
      `var` as a lighter joint macro baseline, `dcc_garch` for time-varying
      liquid-market/crypto volatility and correlations, `wilkie` for
      actuarial long-horizon inflation/equity/rate scenarios, and `bootstrap`
      for empirical stress/history resampling. Do not revive the old
      `macro_market_bundle_provider` shape directly; mine it only for data
      loading/config ideas after the native `SampledMarketBundle` API is the
      target.

## Response wire surface

- [ ] **Extend `ReportSpec` `include_*` gates to the smaller response
      fields**. `include_funding_decisions` / `include_obligations` /
      `include_settlement_results` / `include_failure_events` shipped
      default-off (commit `96537e7d`); follow up with
      `include_market_observations` (~30 MB), `include_projection_trajectories`,
      `include_effects`, `include_policy_decisions`, `include_tax_lots`,
      `include_lot_dispositions`, `include_accounting_details`,
      `include_liabilities`. None are read by any current frontend; one
      backend test (`test_scenario_set_response_serializes_discriminated_effects`)
      reads `market_observations` via the response and would need to opt in.
- [ ] **Server-side result persistence + slicing.** Sketch in
      `augur/plans/persistence_and_slicing.md`. Cache `SimulationRun` /
      `ProjectionRun` data
      keyed by `(seed, scenario_input_id, market_request_hash)` (all already
      content-addressed); expose `/api/runs/<id>/monthly_columns`,
      `/api/runs/<id>/rollout/<i>/series/<metric>`, etc. so the frontend can
      fetch slices on demand instead of getting everything every time. This
      makes "I changed one knob, re-simulate" essentially free and lets the
      debug streams ship without rebuilding the whole response on each call.

## Step 7 Scope

- [ ] Make arrays derive from state/ledger where practical. Where an array remains bespoke for performance or UI compatibility, assert that it reconciles to the ledger total and document any intentional difference.

## API / Runtime Design Debt

- [ ] Keep actor policy execution order-first as policy families grow. New
      policies should run through ordered actor policy programs and emit
      inspectable decisions/traces rather than reintroducing per-class monthly
      loops.
- [ ] Split private-equity sale opportunity, user participation preference,
      policy decision, accounting application, and public action into separate
      concepts with explicit cause IDs. Tender-eligible private marks are not
      liquid assets and must stay out of `liquid_net_worth`.
- [ ] Extend policy schema/programs enough for downstream deployments to express concentrated-holding limits, liquidity-sale preferences, tender/acquisition/IPO preferences, and tax preferences without ad-hoc `AugurConfig` fields.
- [ ] Reintroduce partner/co-owner contributions only as explicit tested
      agreements in `augur/sim`, after the sim backend is wired. The old
      `scenario.actorPolicy` / `owner_plus_partner` product path is shelved:
      "agent X pays agent Y this amount over this period and receives this
      equity/share/claim in return" should be a modeled agreement between
      agents, not a scenario-level enum that activates a hardcoded
      partner-ownership hack.
- [ ] Separate rollout stochastic inputs in the API/data model. Today a rollout
      is effectively a sampled exogenous trajectory plus deterministic policy.
      Make that explicit, and consider separate structures/identifiers for
      market nondeterminism, policy nondeterminism, and any future non-market
      random events so trajectory IDs do not conflate different sources of
      randomness.
- [ ] Keep production stochastic path generation behind the `augur/model`
      trajectory-bundle boundary. The durable simulator contract should consume
      materialized exogenous paths plus provenance rather than fitting or
      sampling markets itself.
- [ ] Rename exogenous-path terminology away from "market price" where it
      covers non-price world-state series such as rent, CPI/inflation, rates,
      home-value indexes, and boolean event paths. The generic boundary should
      read as external series / series values, while consumers interpret a
      series as unit value, rent index, inflation index, rate path, or event
      stream as appropriate.
- [ ] Decide whether negative cash is allowed only through explicit borrowing.
      If the model says an actor has overdraft, credit-line, margin, or other
      borrowing capacity, negative cash can be an accounting effect paired with
      that liability/financing state. Otherwise, a cash shortfall should invoke
      sale/financing policies or mark the corresponding obligation settlement
      failed, rather than silently implying borrowing.
- [ ] Consider making account IDs globally unique so transfer/event frames do
      not need separate `agent_id` + `account_id` columns everywhere. The
      current pair-key shape is explicit but creates boilerplate across cash
      transfers, mortgage payments, tax payments, and future obligations.
- [ ] Keep rollout health machine-readable through `RolloutStatusType`, failure
      events, obligations, funding decisions, and settlement results. Do not
      reintroduce an enum-like `status_reason` string.
- [ ] Clarify initial state vs scheduled transitions. Property purchase,
      financing, ownership, future sale, rental transition, and private-stock
      sale opportunities should not be split across fields/events that can
      contradict each other.
- [ ] Reduce single-property/global assumptions. Scenario-level `property_selection`, `financing`, `rental_plan`, and `tax_profile` should eventually become initial positions, per-property settings, or per-actor/accounting inputs as the simulator grows.
- [ ] Prefer Pydantic for serde and validation at API/config boundaries. Avoid
      custom `to_json_dict()`-style conversion helpers except at narrow
      compatibility seams.
- [ ] Keep the macro market config file boundary pinned by contract tests. New
      fields should update `augur/model/market_config_test.py`, remain
      Pydantic-parsed at load time, and avoid stale simulation knobs already
      owned by `MarketRequest`.
- [ ] Persist and harden model-governance artifacts for market providers. The
      runtime now attaches typed model card, evidence, calibration, scenario
      generator, path-set, and validation-report identities; the next step is
      durable evidence/calibration artifacts, real validation reports, and
      reviewed limitations rather than placeholder IDs.
- [ ] Move evidence/model-fetching shapes out of simulator APIs when touched.
      The simulator should consume calibrated trajectory/provider inputs, not
      source-specific evidence objects.

## Tax Follow-Ups

- [ ] Continue the annual federal + California tax model beyond sale taxes:
      qualified dividends, short-term gains, capital losses, rental income,
      deductible expenses, passive-loss release, SALT/property-tax treatment,
      California conformity/non-conformity, and ordinary income schedules
      beyond one annual `TaxProfile` value.
- [ ] Underpayment-penalty calculation on estimated taxes (interest on
      quarterly shortfalls). The estimated-payment obligations landed via
      #1592; the penalty calc (IRS short-term rate + 3% on the shortfall)
      is the missing piece.
- [ ] Keep stock-sale, PE-sale, and property-sale tax reconciliation in the
      Step 7 test set.

## Reporting / UI Follow-Ups

- [ ] Reframe the Augur UI around the user's conceptual model instead of the
      simulator's current implementation seams. The present sidebar mixes initial
      holdings, policy knobs, exogenous market events, tax constants, financing
      assumptions, actor participation, and result summaries in ways that make the
      product feel internally confused. Start with an information architecture pass:
      define scenario identity, actors/ownership, initial balance sheet, market
      assumptions, policy choices, tax assumptions, and output diagnostics as
      separate concepts before continuing local control tweaks. Keep browser
      state sectioned and typed so the app does not recreate spreadsheet-style
      coupling while mapping into backend objects.
- [ ] Add explicit comparison views for deltas between two scenario
      distributions. The simulator should not bake a baseline/counterfactual
      into each rollout; a delta view should compare two real scenarios, either
      as the distribution of differences between samples from both
      distributions or as paired differences conditioned on the same underlying
      exogenous path.
- [ ] One Mantine holdout: the inline color swatch in `ScenarioList` rows.
      Mantine `ColorInput` is full-width text-plus-swatch and doesn't fit
      the compact inline use; the editable `ColorInput` is already present
      in `SelectedScenarioControls` for the selected scenario. Decide
      whether to invent a compact Mantine swatch wrapper or leave native.
- [ ] Extract shared browser visual-test utilities for deterministic Playwright
      runs. Augur visual goldens currently carry their own Chromium determinism
      flags and injected determinism CSS; those should move into a shared repo
      helper alongside similar browser visual tests so each app does not invent
      its own deterministic screenshot harness.
- [ ] Finish financing-control cleanup around mortgage terms. The browser now
      hides custom term/rate fields outside custom override mode, but the broader
      domain model still needs a pass over which mortgage products and override
      fields should exist at all.
- [ ] Give room-rental vacancy a realistic default in fixtures and deployment
      config. Vacancy now correctly models as a multiplier on rent collected
      (no separate vacancy-loss debit account), so the default value should
      represent an actually realistic occupancy assumption rather than `0%`.
- [ ] Reorganize tax controls so capital-gains rates, exclusions, and other tax
      constants live together and apply consistently to stock, private-equity, and
      property-sale gains. Verify the current math before moving controls.
- [ ] Browser/UI for the new PE `LiquidityRegime` variants. The schema
      now supports `LiquidityEventOnly` / `PublicMarket(lockup_end_month)` /
      `Acquisition(event_month, cash_per_unit_usd)` (#1601), and the engine
      respects all three. The browser still only exposes a single
      "tender-eligible" PE input shape — no UI for setting the regime,
      no UI for entering a lockup or acquisition event. Wire it through.
- [ ] Much later: add real-dollar reporting as a postprocessing/read-model
      layer. The backend and simulator should count in nominal dollars through
      the sim cutover; if/when inflation-adjusted views are useful, add a
      reporting toggle that clearly labels nominal future dollars versus real
      dollars without changing simulator accounting.
- [ ] Clean up redundant result-mode chips. Once a page-level header clearly
      says the user is looking at a distribution or a trajectory, individual
      child cards should not keep repeating `DISTRIBUTION`/`TRAJECTORY` badges
      unless the badge disambiguates mixed content.
- [ ] Normalize result table labels. Headers like `P50 net worth` next to
      `liquid worth` are inconsistent because the second value is also a
      percentile, and "liquid worth" is unclear wording. Prefer explicit,
      consistent labels such as `P50 liquid net worth` or whatever term the model
      settles on.
- [ ] Add hover-driven trajectory inspection to distribution charts. The fan
      chart should keep showing distribution envelopes and the central
      median/mean line by default, but hovering should reveal the selected
      rollout trajectory line without permanently rendering every rollout. While
      hovered, distribution-page summary numbers should switch where meaningful
      from aggregate values to that rollout's values, and time-sensitive values
      should use the hovered x-coordinate/month. The hover detail should also
      show the hovered trajectory's percentile at that point and, if practical,
      lightly highlight the complementary percentile trajectory/range.
- [ ] Restore richer distribution fan rendering. The current chart shows one
      envelope range plus one middle line, but the earlier private prototype had
      a fuller continuous-color fan showing more of the distribution. Consider
      bringing that back after the distribution/trajectory structure settles.
- [ ] Rework selected-path ledger detail toggles. The current chips above the
      ledger table are clunky UI sugar; a better shape would make aggregate
      columns expandable in place, similar to hidden columns in a spreadsheet:
      e.g. `House costs total` expands in place into tax, insurance, HOA, and
      maintenance subcolumns under the same table header.
- [ ] Refresh `augur/SPEC.md` once policy execution, sale taxes, and one-rollout detail have stabilized.
