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

## Priority 3: Sampled Private-Equity, Sampled Tender Timing, And Crypto

Today the market provider holds private-equity marks **flat over the entire
horizon** (`private_equity_value_multipliers = np.ones(shape)`) and emits
tender opportunities at **deterministic** months (every 12 months from t=0,
identical across rollouts and across PE assets). Crypto holdings are dropped
from `to_initial_balance_sheet()` entirely — the asset class doesn't exist
in the runtime universe.

That's three major sources of variance the simulator silently ignores. A
distribution over outcomes that has no PE price uncertainty and pre-known
tender months is not a real distribution over PE outcomes.

### Gaps to close

1. **PE valuation should be sampled.** Per-asset price paths keyed by holding
   identity in `MarketBundle`, persisted via `MarketBundleMetadata`. The
   model is **open design work** — the user's available evidence is sparse
   (5-10 historical OpenAI tenders), so the right fit is likely a joint
   model with SP500, inflation, and per-location housing (currently jointly
   modeled by VECM/VAR/Wilkie/etc.) rather than an independent process per
   PE asset. No specific algorithm is baked in here; the followup picks one
   after a calibration pass on real evidence.

2. **Tender timing should be sampled.** Replace the deterministic 12-month
   mask with a fitted arrival process. Again **open design**: fit the tender
   arrival rate (and any regime conditioning) jointly with the price model
   on the same sparse evidence. The explicit `PrivateEquityLot.tender_windows`
   path landed earlier covers cases where the deployment knows specific
   future windows; this followup adds a sampled fallback for the open-ended
   horizon.

3. **Crypto should be modeled.** Runtime asset class + funding-policy
   surfacing landed via #1582. The remaining gap is sampling a per-asset
   price path so crypto contributes real variance instead of riding a flat
   `np.ones(...)` placeholder. Model choice is open; joint vs independent
   fit is part of the same design pass as the PE side.

### Calibration & evidence boundary

Per-asset PE / crypto paths and tender-arrival rates are model inputs that
should live in `augur/model/`, not `augur/core/`. Result metadata declares
which calibration artifact the run used. Calibration evidence is private
(it's specific deployment data), so the fitted models stay downstream while
the generic framework exposes a typed "sampled PE / crypto / tender" model
contract.

### Result-layer separation (unchanged from prior framing)

Distinguish private-equity mark value, tender-eligible value, actually-sold
amount, post-tax proceeds, and actual liquid net worth. `liquid_net_worth`
stays cash + public liquid securities — never tender-eligible PE marks.

### Acceptance

- A scenario with two PE holdings on independently sampled paths produces a
  non-zero correlation deviation between them across rollouts.
- Two rollouts of the same scenario have **different** tender months for
  the same PE asset (proving timing is sampled, not fixed).
- A scenario with crypto holdings has crypto price uncertainty in the
  distribution over net-worth outcomes; selling crypto records realized
  gain through the same tax flow as stock sales.
- The reason for a sale or non-sale is still inspectable as a policy
  decision with explicit cause IDs.

### Policy / sale machinery (unchanged from prior framing)

- Market/model layer emits private-equity sale opportunities: tender,
  acquisition, IPO/regime change, lockup expiry, public-market availability.
- Policy layer decides participation: never sell, sell fixed fraction, sell
  fixed units, sell enough to reach concentration/liquid-reserve target, or
  custom downstream rule. The first concrete browser/core rule sells a fixed
  amount into SP500 when cash plus public stock falls below a configured
  floor and a tender opportunity exists.
- Accounting layer applies sale, basis, tax estimate/liability, proceeds
  destination, and cause IDs.

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

The simulator executes ordered actor policy programs. The `Instruction` (policy
intent) → `Effect` (realized state mutation) split landed via #1591; `Effect`
rows are now the user-visible trace surface for sales, and ledger/snapshot/
accounting-detail rows are the canonical source of truth for everything else.

Remaining work:

- Add richer policy execution trace rows for no-op, rejected, instructed, and
  applied decisions where trajectory inspection needs them (today the trace
  records the realized `Effect`, but a policy that decided "no sale because
  no opportunity" produces no row).
- Make result inspection typed and local: distribution helpers, trajectory
  helpers, ledger/detail helpers, and compatibility aliases only where needed.
- Keep market paths and exogenous opportunities as observations, not policy
  decisions.

Acceptance criteria:

- Policy order is explicit and testable.
- No policy family bypasses the ordered actor program dispatcher.
- Policy decisions (including no-op / rejected) are visible in trajectory
  inspection.
- Result arrays are not the only way to understand why something happened.

## Priority 6: Property, Location, And Asset Storage

This track keeps the generic framework public-safe and makes downstream
deployment less ad hoc.

Work:

- Keep the generic Augur OCI image free of private config, property records, and
  private media.
- Add a durable property-asset storage contract with stable asset IDs/URLs,
  backed by object storage or a database-like asset table.
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

## Next Lanes (parallelism + sequencing)

- **Priority 3 — sampled PE / sampled tender timing / sampled crypto +
  sampled mortgage rate** (open design work; see the priority section
  above). Joint fit with SP500 / inflation / per-location housing factors
  on sparse evidence. Lives entirely in `augur/model/`, isolated from
  `augur/core/scenario_engine.py`. **The biggest remaining variance
  source the simulator silently ignores.**
- **Tax surface beyond sale tax** — qualified dividends, short-term gains,
  capital losses + carryforward, rental income tax, SALT/property-tax
  deductions, passive-loss release. Most valuable once Priority 3
  produces dividend-like income streams.
- **`RegimeChange` mid-rollout events** — IPO converts
  `LiquidityEventOnly` → `PublicMarket`. The discriminated-union shape
  already supports it; runtime needs to sample the event month and flip
  the variant.
- **Underpayment penalty on quarterly estimates** — IRS interest rate +
  3% on shortfalls. Layers on the year-end true-up that #1592 landed.
- **Borrowing facilities** — overdraft, margin, credit line as explicit
  funding sources in the obligation pipeline. Today negative cash is a
  silent warning; with explicit borrowing it becomes an
  accounting-tracked liability paired with a funding source.
- **Persist model-governance artifacts** — durable evidence / calibration
  / validation-report storage for market providers. `augur/model/`.
  Self-contained, can run in parallel with anything.
- **Replace `scenario.actorPolicy` enums with modeled actor agreements**
  (Priority 2). "Agent X pays agent Y this amount over this period for
  this share/claim" should be a contract, not a scenario-wide enum.

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

### Plan C: Unified Obligation/Funding Semantics (complete)

Plan C is **done**. Every immediate cash demand now flows through the
unified `_settle_required_cash_obligations` pipeline: annual tax,
estimated tax (quarterly with safe-harbor), mortgage, property tax, HOA,
insurance, maintenance, outside rent, partner contribution, special
assessment. Each obligation type has a `required: bool` flag, emits
settlement rows, and produces `FailureEvent` + `RolloutStatusType.FAILED`
on shortfall. Two follow-ons remain, both deliberately scoped out of Plan
C and tracked in `TODO.md`:

- **Underpayment-penalty calculation** on estimated-tax shortfalls (IRS
  short-term rate + 3% on the under-paid amount).
- **Discretionary-obligation deferral** semantics — every variant is
  currently `required=True`; flipping any to discretionary (deferral
  rather than failure) is a future PR.

### Plan D: Keep Market Configuration Typed At The Boundary (guardrail)

Keep the macro market config Pydantic-parsed at load time, with
`market_config_test` as the review point when adding new source-data
fields or deployment-supplied config. Reject stale simulation knobs at
the file boundary — `MarketRequest` owns rollout count, horizon, and
seed; the market config should not keep a second inert copy.

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
