# Augur TODO

Last design scan: 2026-05-17. Last consolidation: 2026-05-17.

This file tracks public, generic Augur backlog. Downstream repos should keep
private composition, deployment, and user-/company-specific modeling assumptions
in their own trackers.

Priority ordering and cross-repo consolidation live in
`plans/roadmap.md`. Keep this file as the public generic backlog rather than a
second ordered roadmap.

## Next

- [ ] **PE should actually be simulated** (Priority 3 in `plans/roadmap.md`).
      Today the market provider holds private-equity marks flat at 1.0 for
      the entire horizon and emits tender opportunities at deterministic
      month indices (every 12 months from t=0, identical across all
      rollouts and all PE assets). The fit is **open design work** —
      available evidence is sparse (e.g. ~5-10 historical OpenAI tenders),
      so the natural shape is a model fit **jointly** with SP500,
      inflation, and the per-location housing factors that the macro
      provider already estimates (VECM/VAR/Wilkie/etc.), rather than an
      independent process per PE asset. Tender-frequency arrival rate is
      fitted on the same evidence.
- [ ] **Crypto price should be sampled.** Runtime asset class + funding-
      policy wiring landed via #1582; the remaining gap is replacing the
      `np.ones(...)` placeholder `crypto_value_multipliers` array with a
      sampled per-asset path so crypto contributes real variance to the
      distribution. Model design is part of the same pass as PE above
      (joint vs independent fit is a design question; deployment evidence
      is private and stays downstream).
- [ ] Plan C remaining slices (foundation landed in
      `claude/plan-c-unified-obligations`): the `ObligationType` enum now
      covers every variant called out in `plans/roadmap.md` (annual tax,
      estimated tax, mortgage, property tax, HOA dues, insurance,
      maintenance, outside rent, special assessment, partner
      contribution); `_CashDebitObligationKind` provides the generic
      pipeline shape with a `required: bool` knob; `SpecialAssessmentEvent`
      is wired end-to-end as the first non-tax / non-mortgage cash demand
      that routes through `_settle_required_cash_obligations`. Still
      pending: (a) refactor `apply_property_operating_cash_flows` so
      property tax / HOA / insurance / maintenance emit obligations
      instead of debiting cash directly; (b) route the contributing-side
      partner contribution through the obligation pipeline so a
      cash-strapped partner produces FAILED; (c) add quarterly estimated
      tax obligations on the standard Apr/Jun/Sep/Jan-next schedule with
      100%/110% safe-harbor (90% first year), with the year-end true-up
      reduced by estimated payments actually made; (d) outside-rent
      obligations for `OccupancyMode.OWNER_RENTS_ELSEWHERE` (currently
      not modeled — leave a tombstone at the call site until rent
      modeling lands).
- [ ] Drop the `Simulation` prefix from internal class names. Inside a
      simulator every class is by definition simulated; the prefix adds
      noise without disambiguating. Survey hit (after trace-surface collapse
      lands): `SimulationAction`, `SimulationPolicyDecision`,
      `SimulationMarketObservation`, `SimulationJournalEntry`,
      `SimulationPosting`, `SimulationBalanceSnapshot`,
      `SimulationAccountingDetail`, `SimulationObligation`,
      `SimulationFundingDecision`, `SimulationSettlementResult`,
      `SimulationFailureEvent`, `SimulationResult`, `SimulationRun`,
      `SimulationTerminal`, `SimulationValidationError`, plus the
      `_SimulationTraceBase` / `_SimulationActionBase` family. All
      6 files are in `augur/core/`; the wire JSON keys do not carry the
      prefix, so this is a purely internal rename.
- [ ] Make the generic Augur OCI image public-safe: no private Python config, property records, or media in image layers; deployments supply private config and assets through mounted runtime inputs.
- [ ] Add a durable property-asset storage contract: stable property asset IDs/URLs backed by object storage or a database-like asset table, so deployments do not need to bake private media into frontend images.

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
- [ ] Replace `scenario.actorPolicy`-style enums with explicit actor
      agreements/contracts. For example, "agent X pays agent Y this amount over
      this period and receives this equity/share/claim in return" should be a
      modeled agreement between agents, not a scenario-level enum that activates a
      hardcoded partner-ownership hack. The exact representation still needs design.
- [ ] Separate rollout stochastic inputs in the API/data model. Today a rollout
      is effectively a sampled market environment plus deterministic policy. Make
      that explicit, and consider separate structures/identifiers for market
      nondeterminism, policy nondeterminism, and any future non-market random
      events so trajectory IDs do not conflate different sources of randomness.
- [ ] Decide whether negative cash is allowed only through explicit borrowing.
      If the model says an actor has overdraft, credit-line, margin, or other
      borrowing capacity, negative cash can be an accounting effect paired with
      that liability/financing state. Otherwise, a cash shortfall should invoke
      sale/financing policies or mark the corresponding obligation settlement
      failed, rather than silently implying borrowing.
- [ ] Keep rollout health machine-readable through `RolloutStatusType`, failure
      events, obligations, funding decisions, and settlement results. Do not
      reintroduce an enum-like `status_reason` string.
- [ ] Clarify initial state vs scheduled transitions. Property purchase,
      financing, ownership, future sale, rental transition, and private-stock
      sale opportunities should not be split across fields/events that can
      contradict each other.
- [ ] Reduce single-property/global assumptions. Scenario-level `property_selection`, `financing`, `rental_plan`, and `tax_profile` should eventually become initial positions, per-property settings, or per-actor/accounting inputs as the simulator grows.
- [ ] Replace built-in `LocationId` enum with database-like location entities, parallel to properties. A location should carry regulation/tax/modeling knobs that downstream regulation and tax code interprets, not require hardcoded enum extension.
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
- [ ] Stop requiring private-equity input positions to carry both `units` and a
      marked `value_usd` when the value is determined by units plus the private
      equity price model. The browser no longer stores an editable private
      equity value, but the backend request still has to derive `value_usd` to
      satisfy the generic asset-position schema. Clean this up in the backend
      position/API model so simulation owns the mark.
- [ ] Move evidence/model-fetching shapes out of core simulator API when touched. Core should consume calibrated market/provider inputs, not source-specific evidence objects.

## Tax Follow-Ups

- [ ] Continue the annual federal + California tax model beyond sale taxes:
      qualified dividends, short-term gains, capital losses, rental income,
      deductible expenses, passive-loss release, SALT/property-tax treatment,
      California conformity/non-conformity, and ordinary income schedules
      beyond one annual `TaxProfile` value.
- [ ] Underpayment-penalty calculation on estimated taxes (interest on
      quarterly shortfalls) once the estimated-payment obligations land in
      Plan C.
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
- [ ] Continue migrating Augur UI controls to Mantine. Mantine now backs the
      app provider, result tabs, result disclosure, all tables (via Mantine
      `Table` with `unstyled` so global typography rules still apply), and
      remaining click-targets that were bare `<button>` (now Mantine
      `UnstyledButton`). One control still native: the inline color swatch in
      `ScenarioList` rows — Mantine `ColorInput` is full-width text-plus-swatch
      and doesn't fit the compact inline use; the editable `ColorInput` is
      already present in `SelectedScenarioControls` for the selected scenario.
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
- [ ] Finish the private-equity tender/opportunity redesign. The browser no
      longer exposes arbitrary USD sale controls, core no longer has a manual
      sale-request path, `liquid_net_worth` no longer counts tender-eligible
      private marks, and a first liquid-net-worth-floor sale policy records
      explicit sale/non-sale reasons. The model still needs richer exogenous
      tender/acquisition/IPO opportunity settings, participation policies beyond
      the first floor rule, and clearer private-stock sale/tax vocabulary.
- [ ] Add a top-level reporting toggle for nominal vs inflation-adjusted USD.
      Amounts, charts, tables, and summary metrics should make clear whether
      they are shown in nominal future dollars or real/inflation-adjusted
      dollars.
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
- [ ] Key partner-equity reporting by partner actor or make it derivable from actor-keyed ledger entries. Aggregate scenario arrays are fine for charts but should not be the only public detail.
- [ ] Reconsider whether to reintroduce per-component partner contribution reporting for interest, property tax, insurance, HOA, and maintenance.
- [ ] Refresh `augur/SPEC.md` once policy execution, sale taxes, and one-rollout detail have stabilized.
