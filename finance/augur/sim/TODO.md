# Augur Sim TODO

Future-facing tracker for known incomplete pieces and follow-ups in `augur/sim`
and the layers immediately around it (product translator, wire, frontend).
Anything fully shipped is removed — git history is the record of done work.

## Architecture / cutover

- **TLH follow-ups.** Pieces 1 (capital-loss netting + carryforward, #1846) and 2
  (reduced-form harvest process, #1881) shipped. Remaining, in rough priority:
  - **Re-fit the decay annually.** The `reduced_form_tlh` decay params are
    `[HEURISTIC]` — the account opened in 2025, so there's no prior-year 1099-B to
    fit decay; the curve shape is an external prior ([VANGUARD-2024]). As TY2026+
    forms arrive, fit the decay; replace the heuristic with a fitted rate once ≥2
    forms exist. The TY2025 anchor was roughly 5%/yr gross harvested loss,
    essentially all short-term.
  - **`representative_sleeve_tlh` variant (plan option #3 — the honest model).**
    A sibling of the `reduced_form_tlh` discriminator: ~5–10 representative sleeves
    (index factor + scaled idiosyncratic noise) running real FIFO harvesting on
    sub-lots, so losses emerge from actual below-basis names instead of a calibrated
    yield. Build only if a decision turns on harvesting behavior in a regime unlike
    the TY2025 calibration window. (Never build option #4, the full factor model.)
  - **Surface `tlh_cumulative_harvest` in output frames.** It's internal engine
    state today (the per-`(policy, rollout)` basis give-back accumulator); add a
    codec/decoder if we want harvested-loss visibility in the rollout detail / UI.
  - **Wash-sale gate.** Not modeled — TY2025 had zero wash sales, so the continuous
    replacement-buying assumption holds; revisit if the sleeve's behavior changes.
  - References for the reduced-form shape: Chaudhuri, Burnham & Lo, "An Empirical
    Evaluation of Tax-Loss-Harvesting Alpha" (Financial Analysts Journal 76(3),
    2020), and Vanguard, "Tax-loss harvesting: Why a personalized approach is
    important" (July 2024).

- Replace the `dense.decode()` → `SimulationRun` polars materialization
  step on the rollout-detail endpoint with `ProjectionRun` read models
  (`augur/sim/projections.py`). The metric-fan path already bypasses
  polars (`monthly_metric_arrays` returns numpy direct from
  `dense.buffers.*`); rollout-detail still calls `dense.decode()` to
  materialize event-log polars frames before the product layer projects
  them. The durable API should expose compact scenario metadata +
  distribution-first projections instead of the `SimulationRun` shape
  entirely.
- Make liquidity policies an account-keyed simulator program internally.
  Config/wire shape can stay list-friendly, but the runtime/compiler
  should consume `{(agent_id, account_id): policy}` so "one policy per
  cash account" is encoded by data shape, not validators.
- Re-enable partial property ownership only as an explicit co-owner /
  partner-equity model. The old scalar `ownership_pct` surface was removed
  because it did not scale cashflows, property taxes, depreciation, sale
  proceeds, mortgage payoff, §121/§1250, or gain routing consistently. A
  future version should define owner shares, liability responsibility,
  contribution ledgers, tax allocation, and sale proceeds together with
  focused simulator tests before exposing fractional ownership again.
- Define the month-0 anchoring rule for every model-driven level series
  in one place. Implementations exist (security, home_value, rent,
  PE, inflation), but no single document says whether a configured value
  is a sim-month-0 level or a fixed contract value.
- Replace float dollar/share accounting in the dense engine with integer
  cents/share-quantum accounting where possible. The FIFO dollar-sale path
  currently has a small dollar-space snap before ceiling whole-unit sales so
  float32/JAX arithmetic does not turn exact ratios like `$50,000 / $500` into
  a spurious extra share. That tolerance should disappear once obligations,
  proceeds, cash, and share quantities operate on explicit integer quanta.
- **Arrays reconcile to ledger.** Monthly result columns should remain
  charts, not truth. Keep shrinking bespoke explanatory array math
  without changing monthly-column semantics:
  - True state snapshots (cash, public asset value, private-equity mark,
    tender-eligible PE value, property value, mortgage balance,
    home-equity claims, net-worth metrics) sourced from state snapshots
    rather than transaction ledger rows.
  - Transaction-flow arrays derived from ledger rows where practical.
    Likely next targets: purchase-closing costs, property depreciation,
    and tax payment timing once the tax ledger/liability shape exists.
  - Explanatory arrays moved toward typed accounting detail once their
    semantics are explicit enough. These arrays explain calculations;
    they should not pretend to be cash movement unless a corresponding
    ledger row exists.
  - Generalize the ledger-derived matrix helper only when the next
    family needs multiple categories, actor filters, property filters,
    or balance snapshots.
  - Keep existing monthly columns stable and keep reconciliation tests
    as guardrails while the implementation source changes.
  - Add any missing causes/IDs needed by derivation. Do not add ad hoc
    string parsing to recover meaning from categories.

## Product UX

- **Deterministic lifecycle markers on the fan chart.** Today
  `SetRentedFractionEvent`, `CapitalImprovementEvent`, and
  `PropertySaleEvent` markers only appear on the per-selected-rollout
  chart. Because they fire at fixed months for every rollout (the
  scenario is deterministic at the lifecycle level), the aggregate
  `MetricFanChart` should also draw a vertical guide at each event's
  month, with a tooltip / legend entry showing the kind. Sketch:
  thread the lifecycle events into `MetricFanChart` alongside the
  per-rollout list; render a separate marker layer keyed on
  `month_index` only. Skip cap-improvement amounts in the fan tooltip
  (use the per-rollout panel for that) — the fan markers are just
  "something happens here" guides.

## Tax

- Reintroduce ordinary income / W-2 sources when a scenario needs
  pre-retirement earning. Currently deferred (priority note 2026-05-24):
  the product targets asset-spend-down retirement projections. MID ships
  visible through CA-side deduction on capital-gain sales without an
  income knob. Promote when a scenario requires earning during the
  horizon.
- Prior-year tax for federal/CA estimated quarterly payments. The
  underpayment-penalty path is explicitly deferred (see "Explicitly
  Deferred"), but quarterly estimates affect cashflow timing and aren't
  modeled.
- NIIT (3.8% on investment income above thresholds) and filing statuses
  beyond single. Out of scope until a scenario surfaces them.

## Rental tax — Phase 2 done

Landed end-to-end: rental income taxed as ordinary; property-linked rental
income plus management/leasing fees are gated by property ownership lifecycle
and decode as transfer events; Schedule E deductions for
management/leasing/HOA/insurance/maintenance route via
`deduction_category="ordinary"` on property cashflows + obligations; MID + SALT
scaled by (1 - rented_fraction) at compile time; rented-share of mortgage
interest deducted via engine year-end Schedule E hook; §168 depreciation accrual
on a cumulative state buffer (ready for §1250 recapture in phase 4) + year-end
Schedule E deduction.

## Real-estate lifecycle

The product surface handles month-0 property purchase, mortgage
origination (180/360-term fixed-rate), property tax, HOA, insurance,
maintenance, MID, SALT, landlord rental income, and mid-horizon
rented-fraction / primary-residence / capital-improvement / sale events.
Still missing:

- **Product outside-rent timeline events.** Primary-residence assignment
  already affects §121 qualifying-use state, but outside rent is still one
  flat horizon-long obligation. Changing, ending, or restarting the user's
  outside rent should be explicit scenario state, not implicit behavior derived
  only from owned-property primary-residence assignment.
- **Mid-horizon property purchase** in year N. Today the product knob is
  locked to "buy at month 0 or don't"; the same `PropertyPurchase`
  should eventually plumb to a configurable purchase month.
- **Property-tax: Proposition-13-style 2%/yr assessed-value escalation
  cap.** `property_initial_assessed_value` is set at purchase and never
  escalates. Long horizons (20-30y) progressively understate tax — by
  year 30 assessed value is ~1.81× under the cap, so tax is understated
  by ~45%. Fix: per-rollout state buffer for assessed value, year-end
  step `min(cpi_growth, 2%)`.
- **Property-tax: first-year supplemental assessment** (CA). A purchase
  triggers a prorated supplemental bill for the difference between the
  previous owner's assessed value and the new purchase price, billed in
  addition to the regular bill in year 1.
- **CFD / Mello-Roos special assessments**: annual escalation cap (2%/yr
  in CA); term / sunset modeling (real CFDs end when bonds repay);
  itemized breakdown (real bills list each CFD separately, e.g. Mare
  Island bills show CFD 2002-1 / 2005-1A / 2005-1B). Today
  `Location.annual_special_assessment_usd` is a flat aggregate.
- **HOA, insurance, maintenance** — knobs exist but model is coarse:
  - HOA is indexed to CPI; real HOAs ratchet in lumpy board votes that
    often outpace CPI. Needs a per-HOA dues schedule.
  - HOA special assessments (roof, seismic retrofit, litigation) are
    not modeled.
  - Insurance is one ScenarioKey-level `annual_insurance_pct` × purchase
    price. Real premiums vary 5–10× by location (CA wildfire
    non-renewals, FAIR-plan fallback, FL hurricane). Move to
    per-location `annual_insurance_pct` once we have a second location
    with real data. No deductible / claim modeling. No replacement-cost
    escalation independent of home value.
  - Maintenance is a flat percentage of purchase price smoothed monthly.
    Real maintenance is lumpy (roof $20-40k, HVAC $10-15k). Tax
    treatment ignored: capital improvements should add to §1250 / §121
    basis; routine repairs should not. Today treated as deductible-free
    cash outflow.

## Private equity

PE protocol series are wired through model → sim → product/API fixtures.
Current support covers voluntary tender/public-market opportunities, sale
capacity, eligibility, liquidity blocks, forced sale fractions, and forced
recovery cashouts. Still missing:

- **Finish the typed PE protocol boundary.** Regime/event-kind codes now travel
  through a typed `private_equity_protocol` frame instead of being parsed from
  float level series. Capacity, eligibility, liquidity-block, forced-sale, and
  forced-recovery controls still travel as numeric auxiliary level series.
  Finish the migration by moving those controls into a dedicated protocol frame
  or equivalent typed control surface, then remove compatibility code-series
  requirements once downstream configs no longer need them for sanity checks.
- **Product/API controls for PE participation preferences** beyond the single
  LNW-floor knob. The sim can consume richer protocol series, but the product
  surface cannot yet express tender acceptance, public-market liquidation,
  acquisition, IPO-lockup, or tax-preference choices separately.
- **Broaden PE path inspection coverage.** Product rollout detail and the
  frontend event layer now expose PE protocol markers and sparse tender
  opportunity rows. Keep adding deterministic tests and production-like sample
  checks for public-market opens, legal impairments, forced recoveries,
  acquisition cashouts, and low-value paths so these remain explainable.
- **`PartnerEquityAccrualPolicy`** translation — depends on the generic
  property-stake model covering partner ownership, contribution
  allocation, and balance snapshots.

## Counterparties & accounts

- The product surface already names counterparty agents (`LANDLORD_AGENT_ID`,
  `MORTGAGE_LENDER_AGENT_ID`, `PROPERTY_SELLER_AGENT_ID`, `HOA_AGENT_ID`,
  `INSURER_AGENT_ID`, `MAINTENANCE_VENDOR_AGENT_ID`, `TAX_AUTHORITY_AGENT_ID`,
  `SPEND_SINK_AGENT_ID`). Future: generalize into a registry of named
  external sinks rather than hard-coded constants, so adding a new
  counterparty doesn't require touching `scenarios.py`.
- Account-type taxonomy is currently `checking` + `taxable_brokerage`.
  When the YAML snapshot needs them: typed `crypto_exchange`,
  `lender`/`loan-side`, and bookkeeping counterparty accounts (most can
  start as metadata + routing, not bespoke settlement logic).

## Exogenous sampling / VECM

- Replace the VECM wrapper's ad-hoc latest-observation lookup with a
  typed evidence-artifact / runtime-state boundary. The model should
  receive factor-keyed current levels and provenance metadata directly,
  not infer security, home-value, rent, and inflation values from
  source-specific `latest_observations` maps. The current branchy
  `_latest_factor_value` (and its per-symbol anchor-name table) is the
  shape of the problem.
- **Mortgage-rate path sampling**. Today the mortgage rate is a single
  PMMS survey number at scenario time; required-series introspection
  doesn't cover a mortgage30 path. Adding it would let "what if rates
  fall to 5% in 18mo" scenarios work.
- **Variable spending / obligation amounts** sourced from exogenous
  paths (today they're either constants or `SeriesIndexedAmount` against
  one model series; richer functional forms aren't supported).
- **Rent cap on `SeriesIndexedAmount`** for rent-control / stabilized
  leases (SF 7%/yr cap, rent-stabilized 3%/yr cap). Currently
  `SeriesIndexedAmount` escalates by the full series ratio without
  bound; outside-rent obligations under those regimes will overstate
  escalation. Surface a `rent_cap_pct` knob on `ScenarioKey` once the
  sim cap lands.
- **Constrained sellability masks E2E**. PE uses the mask to gate
  tender-event-only sales, but the general "lot sellable only when X"
  pattern isn't exercised broadly (e.g. RSU vesting cliffs, ISO holding
  periods, ESPP qualifying-disposition windows).

## Trades & ledger

- **Explicit portfolio trade events**: scheduled asset purchases/sales
  beyond the existing month-0 property purchase. Needs buy-side
  accounting in sim (today only sales are modeled, via liquidity policy
  or PE tender).
- **Ordered policy-program surface** — only if richer decisions need it.
  Existing obligations + liquidity policies cover today's use cases;
  missing behavior should land as typed sim decisions/events with
  explicit cause IDs (not as a generic policy interpreter).
- **Ledger / double-entry read model** — only if consumers need
  double-entry projections. Sim should keep event/state frames as the
  source of truth and derive compact `ProjectionRun` slices first.
- **Declarative posting-schema layer** — only if ledger projections
  need repeated double-entry templates.

## Frontend / API

- Add API serialization, compact scenario metadata, and a frontend
  adapter over `ProjectionRun`. Prefer a clean `model -> sim -> api`
  contract over matching legacy compatibility-table names.
- Expand the backend sim smoke harness beyond the current slices.
  Today `//augur/api:server_test` proves `ScenarioKey` requests
  translate, sample, complete, and return the product response shape;
  it also covers selected PE protocol and capacity-limited tender paths. Keep
  broadening event-stream assertions as new lifecycle surfaces land.

## Refactor follow-ups

- Revisit whether policy should emit all agent actions, including
  obligation-payment transfers. Potential future shape: hard demands
  are inputs to the agent policy; the policy emits both liquidation
  orders and checking-cash payment transfers; settlement only validates
  that every hard demand was satisfied. Current split: policy emits
  sales; settlement emits required payments.
- Consider whether `EventLog` should expose only catalog-keyed access
  (`log.frame(EVENT_FRAMES.transfers)`) or keep the current convenience
  properties (`log.transfers`, etc.). The catalog now owns
  schema/normalization but the property layer still repeats event names
  for caller ergonomics.
- Treat `augur/model/x/legacy_market_models/` as non-runtime code. Port
  only models selected by production or used as representative
  joint-model coverage; delete or keep the rest quarantined.
- Mine the deleted core `PortfolioStatement` for config-ingestion ideas
  only, not as a runtime model. The current user-friendly deployment
  YAML in `augur/api/portfolio.py` may still want custody/source
  metadata, valuation provenance, account references, and tender-window
  metadata.

### Dual-backend (NumPy→JAX) indirection cleanup — done

The NumPy backend was removed in `cdf0ea1fe`; the leftover indirection is now
cleared: naming/comment vestiges and the unused `slice.py` (#1924/#1925/#1926),
and `DenseSimulationResult` collapsed into `SimulationRun` (one lazy run handle
exposing the raw `(plan, buffers, external_series)` triple plus decoded frames).

State buffers intentionally stay **R-last**: the engine's hot rollout math is
memory-bandwidth-bound on the R axis and the metric-fan reduces over R, so R as
the contiguous trailing axis is correct. `r_first_view` is a cheap `np.moveaxis`
view, not a real cost. The actual dense-decode wins are orthogonal to layout —
sparse-active decoding and not storing rarely-read state as a dense per-snapshot
grid; see <augur/debug/rollout_perf_profiling.md>.

## Code review — open findings (2026-05)

Carried over from the 2026-05 code review (`code_review_2026_05_26.md` /
`code_review_2026_05_27.md`, consolidated 2026-05-27). Still matched the current
tree at merge time.

### P2

- **External-series cubes use serial row loops and uneven coverage checks.**
  `_validate_series_indexed_amounts` builds a Python dict from long Polars rows
  and checks rollout/month cells with nested Python loops; `external_values_cube`
  and `external_event_values_cube` fill dense arrays by iterating rows. Coverage
  validation also differs by consumer (`SeriesIndexedAmount` is prechecked; asset
  prices, home values, event cubes can still enter the dense plan as `NaN` /
  default `False`). Share one coverage-checked matrix materialization path for
  level and event series (`simulate.py`, `compiler/series.py`, `model/exogenous.py`).
- **Product fan path splits batched simulations back into one-rollout cache
  entries.** `_simulate_missing` simulates all missing seeds in one dense batch,
  then stores one sliced `DenseSimulationResult` per seed; `_decoded_rollouts`
  and `_metric_matrix` decode N times and restack. Separate distribution/fan
  caching from selected-rollout detail caching (`product/service.py`).
- **Direct cash mutations bypass obligation/failure semantics.** Scheduled
  transfers, property purchases, and capital improvements debit cash directly
  and can push it negative without the hard-demand failure path; behavior is
  defined per phase rather than by a scenario-visible contract. Make the
  cash-demand taxonomy explicit (`engine/phases.py`).
- **Obligation funding recomputes each account group once per slot.**
  `_obligation_group_funded` rebuilds the `(agent, from_slot)` group mask and
  recomputes grouped due per slot. Precompute per-month group ids and compute
  group due once (`engine/phases.py`).

### P3

- **Rollout detail still materializes selected dense results through the old
  frame path** — already tracked as the `ProjectionRun` product cutover above.
- **Lifecycle and obligation discriminators are still raw SoA fields.**
  `LifecycleEventCompileOutput` reuses one `amount` field by `kind`;
  `ObligationCompileOutput` carries `source_kind` / `source_index` with
  kind-dependent payload. Add typed per-kind views over the dense rows
  (`compiler/lifecycle.py`, `compiler/obligations.py`, `engine/phases.py`).
- **Some compile-plan fields still sit outside their natural arenas.**
  `capital_gain_agent_codes`, `tax_profile_capital_gain_index`,
  `property_rented_fraction`, `property_building_basis`,
  `property_owner_profile_index`, `liability_owner_profile_index` remain
  top-level arrays on `CompiledSimulation`. Move when touching nearby compiler
  code; likely homes `TaxCompileOutput` / `PropertyCompileOutput` /
  `LiabilityCompileOutput` (`compiler/plan.py`).

## Future / nice-to-have

- **Stochastic tenant model.** Landlord rental income today uses a
  flat `vacancy_pct` multiplier + leasing fee firing on a fixed
  `avg_tenancy_months` cadence. Since we already have exogenous
  stochasticity wired (VECM joint fit, per-rollout paths), we can
  model individual tenants stochastically: sample tenancy duration
  from a distribution (geometric / Weibull fit to real data), sample
  vacancy gap between tenants, sample rent-roll at each new tenancy
  (could regress against the rent series mean). This converts
  vacancy + leasing-fee timing from a smoothed assumption into a real
  per-rollout stochastic source, useful both for distribution
  variance and for short-horizon scenarios where leasing-fee lumpiness
  matters. Defer until landlord-rental scenarios are common enough
  that the smoothed model bites.

## Funding policy: asset-balance targeting

Today `FundingPolicy` is a cash-buffer + ordered sell list — it only
fires when cash dips below `cash_buffer_trigger_below_usd`. Extend
with a target-balance mode: the user specifies a desired allocation
across liquid asset classes (e.g. 50% stocks / 30% crypto / 20%
cash), the engine periodically rebalances by selling overweight
buckets and buying underweight ones. Needs:

- Wire: `FundingPolicy.target_allocation: dict[bucket, fraction] | None`
  alongside the existing trigger/sell knobs.
- Sim: an `AssetBalanceRebalancingPolicy` (or extend `LiquidityPolicy`)
  that runs on a configurable cadence and emits buy/sell orders to
  reduce drift past a tolerance threshold.
- Tax routing: rebalancing sells realize gains/losses through the
  same FIFO + capital-gain plumbing the cash-buffer path uses.
- Frontend: alongside the existing "Sell preference" list, a
  target-allocation editor (one row per bucket, percentage, must
  sum to 100).

Defer until a scenario actually needs it — single-bucket portfolios
or pure cash-buffer behavior cover the common cases today.

## Funding policy: "reserve for N months" threshold

The old `Config.reserve_forward_months` knob (paired with
`minimum_reserve_mode: projected_deficits`) used to drive a forward-
looking liquidity reserve target — "keep enough cash to cover the next N
months of projected deficits". The fields were removed because no live
consumer read them after the scenario*set deletion, but the \_capability*
is still desirable on the product surface as a `FundingPolicy` knob.

Sketch:

- `FundingPolicy.reserve_months: PositiveInt | None = None` — when set,
  the cash-buffer trigger becomes `sum(next reserve_months months of
scheduled obligations) - expected income`, evaluated per rollout per
  month.
- Engine: in `_apply_liquidity_policy_sales`, compute the forward
  projected deficit from `plan.obligation_due[month .. month+N, ...]`
  (or a precomputed cumulative sum) and use that as the trigger
  threshold instead of the static `cash_buffer_trigger_below_usd`.
- Wire/frontend: dual-mode picker — "absolute $ trigger" vs "N months
  of runway"; defaults to absolute.
- Tax routing: rebalancing sells realize gains/losses through the same
  FIFO + capital-gain plumbing the cash-buffer path uses.

Defer until a scenario actually needs the dynamic threshold; the
absolute trigger covers today's product surface.

## Explicitly deferred

Documented to prevent re-discovery; intentionally not on the roadmap.

- HIFO, specific-id, and average-cost lot selection (FIFO is the only
  cost basis method today).
- Withholding, underpayment penalties, partial obligation payments,
  delinquency balances, grace periods, and failure recovery.
- Globally unique account IDs to remove repeated `agent_id` join
  boilerplate.
- Real-dollar / inflation-adjusted display as a separate accounting
  mode. Should be a postprocessing/read-model layer, not alternate
  simulator accounting.
