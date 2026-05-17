# Augur TODO

Last design scan: 2026-05-17. Last consolidation: 2026-05-17.

This file tracks public, generic Augur backlog. Downstream repos should keep
private composition, deployment, and user-/company-specific modeling assumptions
in their own trackers.

Priority ordering and cross-repo consolidation live in
`plans/roadmap.md`. Keep this file as the public generic backlog rather than a
second ordered roadmap.

## In Flight

- Sale-tax timing slice — move sale-tax obligations off
  `ALLOCATED_TO_SOURCE_MONTH` onto realistic year-end / estimated-payment
  dates. PR #1578.
- Funding policies consume crypto + tender-window-aware PE — extend the
  obligation-funding policy chain so `CheckingFloorSellPublicStockPolicy`
  (and siblings) can liquidate crypto holdings and tender-eligible PE
  alongside SP500. Branch `claude/funding-policies-crypto-tender`.

## Next

- [ ] Use the generated Augur OpenAPI/browser schema target in browser state
      normalization and request mapping. Python Pydantic remains the source of
      truth; do not grow a second hand-maintained Zod/schema definition in
      `augur/frontend`.
- [ ] Continue `plans/e2e_redesign.md` Step 7: quarterly estimated tax payments and underpayment safe-harbor rules on top of the existing year-end annual-tax obligation.
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
- [ ] Make result inspection typed and local. String metric names via `series("cash_usd")` are acceptable as a compatibility layer, but primary callers should get discoverable typed metric/rollout/detail helpers.
- [ ] Separate rollout stochastic inputs in the API/data model. Today a rollout
      is effectively a sampled market environment plus deterministic policy. Make
      that explicit, and consider separate structures/identifiers for market
      nondeterminism, policy nondeterminism, and any future non-market random
      events so trajectory IDs do not conflate different sources of randomness.
- [ ] Generalize rollout failure semantics beyond the first annual-tax
      obligation settlement slice. `RolloutStatusType.FAILED` now hangs off
      unsettled required obligations; extend that pattern to mortgages, other
      tax/payment demands, and future default/termination behavior.
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
- [ ] Move pure-data model inputs, catalog rows, local-regulation/tax defaults,
      and location-to-tax-regime mappings out of app/server Python and into
      typed configuration resources, such as Pydantic-parsed YAML loaded via
      runfiles or `importlib.resources`. Keep Python for loading, validation,
      and composition logic; use behavior tests around conversion/catalog
      output rather than literal YAML mirror tests.
- [ ] Prefer Pydantic for serde and validation at API/config boundaries. Avoid
      custom `to_json_dict()`-style conversion helpers except at narrow
      compatibility seams.
- [ ] Keep the macro market config file boundary pinned by contract tests. New
      fields should update `augur/model/market_config_test.py`, remain
      Pydantic-parsed at load time, and avoid stale simulation knobs already
      owned by `MarketRequest`.
- [ ] Teach runtime funding policies to consume the crypto and
      tender-window-aware private equity positions modeled in the public
      portfolio YAML contract (`augur/core/portfolio.py`,
      `augur/core/testdata/portfolio.example.yaml`). Today
      `PortfolioStatement.to_initial_balance_sheet()` maps cash + generic S&P 500
      lots + opaque PE marks into the existing `InitialBalanceSheet` shape and
      drops crypto holdings and tender windows; the simulator should grow first-
      class handling for those positions instead of only preserving them in
      typed input data.
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
- [ ] Remove redundant `augur_` prefixes from internal module names such as
      `augur.core.augur_accounting`; inside the `augur` package they add noise
      without clarifying ownership.

## Tax Follow-Ups

- [ ] Continue the annual federal + California tax model beyond sale taxes:
      qualified dividends, short-term gains, capital losses, rental income,
      deductible expenses, passive-loss release, SALT/property-tax treatment,
      California conformity/non-conformity, and ordinary income schedules
      beyond one annual `TaxProfile` value.
- [ ] Layer quarterly estimated-payment timing on top of the existing
      year-end annual-tax obligation. Sale tax now accrues per source month
      (TaxPaymentAllocationDetail, `payment_timing=YEAR_END`) and settles in
      a single year-end obligation collapsed onto month index `year * 12 +
11` (clipped to horizon). Quarterly estimated payments (and the
      safe-harbor rules around underpayment penalties) are a follow-on.
- [ ] Prefer yearly income/tax-lot ledgers with explicit tax settlement near
      realistic payment dates over trying to account for every tax effect at
      the moment income or a gain occurs. The settlement workflow should also
      model cash management: pay from cash when possible, otherwise invoke an
      explicit sale/financing policy to raise cash for the tax bill.
- [ ] Promote mortgage payments to the same first-class obligation/funding
      flow as annual taxes. Today `apply_mortgage_payment` debits cash
      unconditionally; insufficient cash silently goes negative instead of
      raising an obligation, invoking a funding policy, and failing the
      rollout when no policy can cover it. The natural pattern is the one
      tax now uses (obligation -> funding decision -> settlement or failure).
- [ ] Keep stock-sale, PE-sale, and property-sale tax reconciliation in the
      Step 7 test set.
- [ ] Remove the unused flat tax-rate fields. `TaxProfile.marginal_tax_rate`
      and `cap_gains_rate` no longer drive any engine computation (the
      bracket-aware annual-tax allocation owns federal + California for
      capital gains and depreciation recapture). The fields are still in
      the schema, browser state, and `apply_private_equity_sale_instruction`'s
      `cap_gains_rate_pct` parameter (always passed `0.0`). Drop them across
      schema, app catalog defaults, browser state, and the policy-runtime
      signature in one atomic change.

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
- [ ] Continue migrating Augur UI controls to Mantine. Mantine is the chosen
      boring React component kit and now backs the app provider, result tabs, and
      result disclosure behavior. Migrate form controls, tables, buttons, input
      groups, and remaining disclosure widgets incrementally instead of adding
      more one-off local primitives.
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
