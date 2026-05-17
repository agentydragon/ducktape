# Augur Unified Plan

Last consolidated: 2026-05-16.

This plan consolidates the active Augur work from the public framework docs and
the private deployment notes. It is the priority ordering. `augur/TODO.md`
remains the detailed public backlog; private values, holdings, property data,
and deployment-specific composition stay in the downstream private repo.

## Sources

- `augur/SPEC.md`: product contract and simulator vocabulary.
- `augur/plans/e2e_redesign.md`: distribution-first runtime redesign and
  ledger/reconciliation work.
- `augur/plans/prior_art_audit.md`: external architecture lessons for path
  identity, governance, policy projection, and accounting traces.
- `augur/plans/cleanup_audit.md`: local stale-path audit and deletion sequence.
- `augur/TODO.md`: public generic backlog.
- `gaffer-private/TODO.md`: private personal-finance modeling follow-ups.
- `gaffer-private/x/augur/SPEC.md`: private deployment boundary and image
  privacy contract.
- `gaffer-private/debug/augur-ui-structural-review-2026-05-15.md`: screenshot
  review and UI/domain-boundary audit.
- `gaffer-private/x/augur/model/legacy_pymc/PLAN.md`: archived only. Do not
  revive this provider without a fresh model-design pass.

## North Star

Augur simulates a `ScenarioSet` across sampled market paths and returns a
distribution over trajectories. A selected rollout is an inspection aid, not a
separate deterministic product. The UI, app state, and result APIs should make
that distinction impossible to miss.

The core model should stay structured around actors, accounts, assets,
liabilities, markets, policies, actions, ledgers, accounting detail, and
balance snapshots. The app may provide friendly controls, but it should not use
a flat browser-side "scenario row" as the source of truth and then expand it
back into typed backend objects.

## Prior-Art Shape For Core Cleanup

The prior-art audit points to a conservative target shape:

- Market generation and household projection stay separate. `MarketRequest` plus
  `MarketBundle` is the economic scenario generator boundary; the core
  simulator is deterministic once it receives a scenario set and sampled
  exogenous paths.
- Trajectory identity includes scenario input, market model identity,
  evidence/calibration identity, generator implementation/version, seed, path
  index, and any non-market event streams. `rollout_index` remains a convenient
  selector, not a reproducibility key by itself.
- Actor policy programs are ordered programs. Policy steps emit decisions and
  instructions; accounting/runtime code validates and applies effects. New
  policy families should not reintroduce per-class execution loops.
- Ledgers, balance snapshots, accounting detail, lots, liabilities, and typed
  cause IDs are the source of truth. Monthly arrays are chart/report views over
  that state, not a parallel semantic model.
- Model governance is part of the model output. Market bundles now carry first
  typed model/evidence/calibration/generator/path identities and model-card or
  validation-report pointers; the next cleanup is to persist real artifacts and
  validation results behind those IDs.

## Priority 1: Type Result Views And Accounting Detail

Distribution and trajectory have separate top-level views. The React app now
uses capability-focused result helper wrappers so child panels ask for
distribution percentiles, selected-rollout rows, or accounting-detail rows
instead of reaching through raw result payloads. The remaining product and
correctness work is to keep that boundary intact while deeper accounting views
and inputs move into their final shape.

Current state and target shape:

- `/inputs` or an equivalent persistent edit surface: scenario identity,
  initial balance sheet, actors/ownership, property/location, financing,
  occupancy/rental plan, tax/accounting assumptions, market assumptions, and
  policy programs.
- Property/location details, financing/tax assumptions, market metadata, and
  accepted scenario contract are shared context, not distribution or trajectory
  output. Keep that boundary as result panels and inputs continue to move.

Implementation notes:

- Keep result panels declared through the shared frontend result-panel contract:
  `distribution`, `trajectory`, or `accounting_detail`. The contract is encoded
  in `data-result-panel-kind`; view-level headers provide the visible
  distribution/trajectory context so child trajectory panels do not need
  repetitive chips.
- Trajectory URLs are reproducible only when the encoded market request has a
  deterministic seed. The locator is effectively scenario-set input plus
  market model/version plus seed plus `scenario_id` plus `rollout_index`; seed
  and rollout alone are not enough.
- The same `rollout_index` should identify the same exogenous market path
  across scenarios in a scenario-set run so trajectory comparison is meaningful.
- Keep report/view knobs honest: `include_monthly_columns` is currently real;
  do not add report selectors or response-shaping fields unless the backend and
  UI actually honor them.

Acceptance criteria:

- Every result panel has a machine-readable mode: distribution, trajectory, or
  accounting detail. The current React app has the panel contract and per-view
  helper wrappers in place; keep extending them as panels split or move while
  keeping visible mode labels at the page/view boundary rather than repeating
  them on every child card.
- No panel combines percentile summaries with one-rollout path rows unless the
  split is explicit and visually separated.
- Scenario/run context is not rendered as distribution or trajectory output;
  it is shown in shared context or a dedicated details/input surface.
- Deltas are result-view comparisons between two real scenarios, not a
  simulator-level baseline inside each rollout. Prefer paired differences when
  both scenarios share exogenous paths; otherwise expose the choice as a
  distribution of sampled differences between scenario distributions.
- Full-page visual goldens cover representative distribution and trajectory
  routes so UI structure changes are reviewable in git.

Component-kit decision:

- Mantine is the standard React component kit for Augur. The app now installs a
  `MantineProvider` and uses Mantine primitives for result tabs/disclosure; keep
  migrating remaining controls to Mantine instead of inventing new local widgets.

## Priority 2: Keep Browser State Structured And Schema-Driven

The flat browser scenario row has been retired from normal app state and
request mapping. URL and browser scenario inputs are nested by domain section,
UI writes go through section-scoped patches, and new URL versions can break
stale state. The next risk is allowing the browser to become an independent
schema owner through hand-written field lists and ad hoc validators.

Target browser state:

- `identity`: scenario id, label, color, enabled state, comparison membership.
- `actors_and_ownership`: primary owner, optional counterparties, ownership
  agreements, actor-specific policy activation.
- `initial_balance_sheet`: accounts, liquid positions, private positions,
  property positions, liabilities, cost bases, units, and lot-level fields once
  available.
- `property_and_location`: selected property, location entity, local
  regulation/tax knobs, property assumptions.
- `financing`: standard mortgage products and explicit custom override mode if
  retained.
- `occupancy_and_rental`: residence and rental-use plan.
- `tax_accounting`: tax rates, filing assumptions, basis assumptions, timing
  assumptions, and approximation disclaimers.
- `market_model`: selected market model, rollout count, horizon, seed, and
  shared-path behavior.
- `policies`: ordered actor policy programs.

Implementation notes:

- URL state does not need stale compatibility unless explicitly requested.
- The browser state and request mapping now consume structured scenario
  sections directly. Do not reintroduce a catch-all flat scenario view or
  wide-row migration path.
- Generate browser-side schema/types from the backend Pydantic/OpenAPI schema
  instead of growing independent hand-written JS schemas. The repo already has
  `//devinfra/js:openapi.bzl` and `//props/frontend/src/lib:schema` as patterns
  for this class of build-time propagation.
- Private-equity initial positions should eventually carry units plus a holding
  or price-model reference, not a duplicated editable `value_usd`. Today the
  browser derives a backend asset value from units only because the generic
  backend asset schema still requires a mark; simulation should own that mark.
- Replace `scenario.actorPolicy` enums with modeled agreements between agents.
  A partner contribution should look like a contract: agent X pays agent Y some
  amount over a period and receives a specified equity/share/claim in return.
  The exact object model is still open, but it should live in actor/ownership
  state rather than as a scenario-wide enum that triggers bespoke runtime code.
- `scenarioSetInputToRequest` should mostly map structured UI state into the
  backend schema. It should stop hiding domain decisions behind unrelated flat
  fields or actor-policy ids.
- App tests should cover current structured state and generated boundary
  validation rather than preserving older wide-row browser contracts.

Acceptance criteria:

- Adding a new tax assumption, asset type, policy, or actor does not require
  another unrelated field on a giant scenario object.
- The app state names the same domain layers the backend schema names.
- Normal app code does not call a catch-all flat scenario view.

## Priority 3: Redesign Private-Equity Tender Opportunities And Policy

Private-equity sale availability should be modeled as an exogenous opportunity
plus actor policy, not as "user chooses to sell USD X in month Y." A
tender-eligible mark is not liquid wealth; `liquid_net_worth` should include
only actually liquid assets such as cash and public stock.

Target shape:

- Market/model layer emits private-equity sale opportunities: tender,
  acquisition, IPO/regime change, lockup expiry, public-market availability.
- Policy layer decides participation: never sell, sell fixed fraction, sell
  fixed units, sell enough to reach concentration/liquid-reserve target, or
  custom downstream rule. The first concrete browser/core rule sells a fixed
  amount into SP500 when cash plus public stock falls below a configured floor
  and a tender opportunity exists.
- Accounting layer applies sale, basis, tax estimate/liability, proceeds
  destination, and cause IDs.
- Result layer separates private-equity mark value, tender-eligible value,
  actually sold amount, post-tax proceeds, and actual liquid net worth.

Implementation notes:

- Split sale opportunity, user preference, policy decision, accounting
  application, and public action into separate typed concepts with explicit
  cause IDs.
- Keep arbitrary manual sale requests out of the browser and core, and do not
  count tender-eligible private marks in `liquid_net_worth`; both are now
  covered by app/core tests. A first liquid-net-worth-floor participation policy
  exists and PE sale policy decisions now carry explicit sale/non-sale reasons;
  keep extending that policy surface instead of reviving manual sale controls.
- Clarify sale-proceeds destination scope and vocabulary as the policy set
  grows. The current liquid-net-worth-floor policy always reinvests proceeds in
  SP500; future policies should make per-policy or per-action destination and
  tax treatment explicit without returning to an ambiguous scenario-wide cell.

Acceptance criteria:

- A private-equity tender appears as an opportunity in a trajectory view, not
  as generally available liquidity.
- The reason for a sale or non-sale is inspectable as a policy decision.
- Distribution summaries can report expected tender proceeds without implying
  the asset is generally liquid.

## Priority 4: Tax, Basis, And Accounting State

Tax and accounting need to become a first-class layer rather than scattered
controls under house, stock, and private-equity panels.

Target shape:

- Initial positions carry basis, units, lots, and owner/account identity.
- Stock-sale, private-equity-sale, and property-sale taxes reconcile through
  shared accounting detail.
- Tax payments become liabilities/payment-timing flows rather than only
  `allocated_to_source_month` adjustments.
- Public tax model remains approximate, with disclaimers and test coverage
  around what is and is not decision-grade.

Draft obligation/settlement shape:

- The accounting layer emits first-class obligations, not policy hooks: actor,
  period, due month/date, amount, creditor/jurisdiction, source ledger entries
  or tax lots, and status. Taxes are one obligation type; mortgage principal,
  interest, escrow, and other scheduled debt payments should eventually fit the
  same modeling universe.
- The obligation is mandatory model state. Actor policy can decide how to fund
  it, but should not decide whether the liability exists.
- Actor policy responds with a funding decision: use existing cash, sell public
  stock, sell private equity if an opportunity exists, borrow, or explicitly
  fail/skip if no available action can satisfy the obligation.
- Actor policy emits instructions: `SELL_SP500`, `SELL_PRIVATE_EQUITY`,
  `BORROW`, `PAY_TAX`, `PAY_MORTGAGE`, or similar. The simulator/accounting
  layer validates those instructions and records resulting effects. Settlement
  then marks the obligation paid, partially paid, unpaid, or failed and records
  the cash and accounting effects.
- This is analogous to the private-equity tender flow but with different
  semantics: a tender is an optional opportunity, while a tax obligation is an
  endogenous cash demand/liability. The common abstraction is not "hook" but a
  typed event/obligation plus an inspectable actor decision, policy
  instructions, and applied effects.

Public work:

- Continue Step 7 by replacing `allocated_to_source_month` timing with annual
  or estimated-payment liability timing.
- Keep arrays derived from state/ledger where practical, or assert and document
  reconciliation where arrays remain bespoke.
- Extend federal and California tax approximations beyond sale taxes only when
  the accounting shape can represent them.

Private downstream work:

- Populate real cost bases for private holdings and taxable brokerage
  positions in the private repo.
- Model managed direct-index/tax-loss-harvesting behavior as a private
  deployment input once the generic position/tax hooks exist.

Acceptance criteria:

- A sale action can be traced to basis, realized gain, taxable gain, tax
  liability, and cash/asset proceeds.
- Tax controls live together and apply consistently across stock,
  private-equity, and property-sale flows.

## Priority 5: Policy Runtime And Result Typing

The simulator should execute ordered actor policy programs and expose typed
inspection surfaces.

Work:

- Keep execution on the ordered actor policy program dispatcher as policy
  families grow; do not add new per-class monthly loops.
- Add richer policy execution trace rows for no-op, rejected, instructed, and
  applied decisions where trajectory inspection needs them.
- Rename or reframe the runtime vocabulary around `Instruction` plus `Effect`.
  In the current accounting-oriented simulator, the actor's RL-like choice is
  closer to a policy decision/instruction, while the existing `Action` concept
  has drifted toward the realized state change after validation and accounting.
- Make result inspection typed and local: distribution helpers, trajectory
  helpers, ledger/detail helpers, and compatibility aliases only where needed.
- Keep market paths and exogenous opportunities as observations, not policy
  decisions.

Acceptance criteria:

- Policy order is explicit and testable.
- No policy family bypasses the ordered actor program dispatcher.
- Policy decisions are visible in trajectory inspection.
- Result arrays are not the only way to understand why something happened.

## Priority 6: Property, Location, And Asset Storage

This track keeps the generic framework public-safe and makes downstream
deployment less ad hoc.

Work:

- Keep the generic Augur OCI image free of private config, property records, and
  private media.
- Add a durable property-asset storage contract with stable asset IDs/URLs,
  backed by object storage or a database-like asset table.
- Replace built-in `LocationId` with database-like location entities when the
  location/regulation layer is next touched.
- Keep large private media out of ConfigMaps. The current private nginx image
  is an expedient until the generic asset contract exists.

Acceptance criteria:

- Public image layers contain only generic Augur code and public-safe inputs.
- Private deployments can supply config, property records, and media through
  runtime inputs without forking app logic.

## Priority 7: UI Cleanup After The Structural Split

These are visible but should follow the distribution/trajectory and state-shape
work so they do not polish the wrong structure.

Work:

- Continue the Mantine migration for boring controls before polishing current
  hand-built Tailwind widgets. Prefix/suffix input adornments, tables, buttons,
  and form groups should move to the chosen component surface unless there is a
  documented reason not to.
- Continue renaming private-equity result columns/panels away from generic
  liquidity language where they mean tender eligibility or sale opportunities.
- Rework mortgage controls around standard mortgage products and explicit
  custom override mode.
- Refresh `augur/SPEC.md` after policy execution, tax timing, and result-view
  contracts stabilize.

## Immediate Implementation Sequence

1. Continue core model cleanup before broad UI polish: account-aware
   obligations/funding, failure/default semantics, and ledger/accounting detail
   as the source of truth for monthly report arrays.
2. Persist and harden trajectory, path, cause, and model-governance identities
   so a selected rollout can be reproduced and audited from scenario input
   through market evidence and policy decisions.
3. Keep expanding ordered actor policy programs through explicit decision and
   instruction traces, now that execution order is the runtime path.
4. Move public generic data toward typed config resources: local
   regulation/tax defaults, catalog rows, market config, and eventually a
   deployment-supplied portfolio/account YAML contract. Private values stay in
   downstream repos.
5. Wire the generated Augur OpenAPI/browser schema target into browser state
   normalization and request mapping, then split app/frontend/server packages
   after the core contracts and server cleanup settle.

## Next Work Plans

### Plan A: Consume Generated Browser Schemas

Scope:

- Wire `augur/frontend/lib/scenario_set_state.js` and tests to consume the generated
  Augur OpenAPI/browser schema target instead of hand-maintaining boundary
  field lists and ad hoc object probes.
- Keep the generated schema target as the only browser-facing API schema source
  of truth; backend Pydantic models define the public payloads.
- Avoid defining an independent Augur Zod schema by hand; if Zod is used, it
  should be generated from the Python schema.

Validation:

- `nix develop --command pre-commit run --files augur/frontend/lib/BUILD.bazel augur/frontend/lib/scenario_set_state.js augur/frontend/lib/scenario_set_state_test.mjs augur/plans/roadmap.md augur/TODO.md`
- `nix develop --command bazelisk --output_user_root=/tmp/bazel-augur-schema-generation test //augur/frontend/lib:scenario_set_state_test //augur/api:browser_shell_test --nocache_test_results --test_size_filters=small,medium,large`

### Plan B: Make Private-Equity Opportunities And Policy Explicit

Scope:

- Rename remaining result labels that imply general liquidity when the model
  only has tender eligibility or sale opportunity value.
- Add stable IDs and row-level observations for private-equity tender
  opportunities.
- Extend policy-decision rows so sale and non-sale reasons are enough for the
  trajectory view to explain each tender.
- Keep `liquid_net_worth` as cash plus public liquid securities.

Validation:

- `nix develop --command bazelisk --output_user_root=/tmp/bazel-augur-pe-plan test //augur/core:test_e2e //augur/core:scenario_engine_test //augur/api:browser_shell_test --nocache_test_results --test_size_filters=small,medium,large`

### Plan C: Tax/Obligation Settlement Slice

Status:

- First slice landed for annual tax obligations: unsettled required obligations
  produce settlement/failure rows and `RolloutStatusType.FAILED`; sale policy can
  rescue the obligation.
- Property-sale tax now flows through the same bracket-aware obligation path
  (2026-05-16). `property_disposition_arrays` reports pre-tax proceeds only;
  the engine drives sale tax exclusively through `annual_sale_tax_allocation`,
  so stock-sale, PE-sale, and property-sale tax share one settlement pipeline.
- Sale-tax obligation timing moved off the source month onto year-end
  (2026-05-17). `TaxPaymentTiming.YEAR_END` is the new default;
  `TaxPaymentAllocationDetail` keeps per-source-month accrual provenance,
  but the obligation that draws cash collapses each tax year onto month
  index `year * 12 + 11` (clipped to the simulation horizon). Property sale
  journal entries no longer post to `TAX_EXPENSE`; the year-end tax accrual
  - settlement journal entries do. The `CheckingFloorSellPublicStockPolicy`
    funding-policy escape hatch still applies at the settlement month.

Scope:

- Layer quarterly estimated-payment timing on top of the year-end
  obligation. Safe-harbor and underpayment-penalty rules are a follow-on.
- Add tests for mortgage/payment shortfall, sale-policy rescue, explicit
  failure/default semantics, and continued-vs-terminated projection behavior.

Validation:

- `nix develop --command bazelisk --output_user_root=/tmp/bazel-augur-obligation-plan test //augur/core:test_e2e //augur/core:scenario_engine_test //augur/core:policy_runtime_test --nocache_test_results --test_size_filters=small,medium,large`

### Plan D: Keep Market Configuration Typed At The Boundary

Status:

- Implemented for the current macro market config. The remaining work is to
  keep the file-contract guardrail in place as source data and downstream
  deployment inputs grow.

Scope:

- Maintain the Pydantic model for the macro market config file and parse JSON
  once at load time.
- Keep `MacroMarketBundleProvider`, evidence loaders, and location market source
  mapping on typed field access.
- Keep `SourceDataConfig`, location-market-source validation, and the checked-in
  example file under the same typed config tree, with no parallel hand-written
  schema or compatibility path.
- Reject stale simulation knobs at the file boundary. `MarketRequest` owns
  rollout count, horizon, and seed; the market config should not keep a second
  inert version of those controls.
- Use the contract test as the review point when adding new source-data fields
  or deployment-supplied config.

Validation:

- `bbr test //augur/model:market_config_test //augur/model/...`
- `bbr test //augur/core:test_e2e`

### Plan E: Server Boundary Cleanup

Scope:

- Replace `AugurBackend` constructor nullability with explicit dependency/config
  objects or separate production/dev factory paths.
- Collapse the one-function static-path helper into the HTTP server boundary
  unless it grows real ownership.
- Keep this as a behavior-preserving server cleanup; defer package moves until
  the server surface is smaller.

Validation:

- `nix develop --command bazelisk --output_user_root=/tmp/bazel-augur-server-cleanup test //augur/api:browser_shell_test //augur/api:config_test --nocache_test_results --test_size_filters=small,medium,large`

### Plan F: Move Location Tax Defaults To Model Config

Scope:

- Move app-owned tax-regime defaults and location-to-tax-regime mapping into
  typed location/local-regulation configuration.
- Keep app catalog/server code as a consumer of modeled location defaults, not
  the place that decides tax semantics.
- Use behavior tests around scenario conversion/catalog output rather than
  literal YAML value change-detector tests.

Validation:

- `nix develop --command bazelisk --output_user_root=/tmp/bazel-augur-location-tax-config test //augur/api:catalog_test //augur/api:config_test //augur/core:local_regulation_test //augur/core:scenario_set_test --nocache_test_results --test_size_filters=small,medium,large`

### Plan G: Split App Package Boundaries

Scope:

- Move browser code and bundle targets from `augur/app` toward an
  `augur/frontend` package.
- Move HTTP/API/server code from `augur/app` toward an `augur/server` or
  `augur/api` package.
- Run this after the server cleanup, Mantine cleanup, and visual-test helper
  lanes land, so the split is mostly mechanical and easier to review.

Validation:

- `nix develop --command bazelisk --output_user_root=/tmp/bazel-augur-app-split test //augur/... --nocache_test_results`
- `nix develop --command bazelisk --output_user_root=/tmp/bazel-augur-app-split build //augur/...`

## Verification Loop

For each public framework slice:

```bash
bbr test //augur/api:browser_shell_test
bbr test //augur/frontend/lib:scenario_set_state_test
bbr test //augur/core:test_e2e
```

Before handing off a broader spiral:

```bash
bbr test //augur/...
bbr build //augur/...
```

For private deployment slices, also run the private Augur browser/backend tests
and verify the live deployment only after the public framework commit is
repinned downstream.
