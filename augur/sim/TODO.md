# Augur Sim TODO

Future-facing tracker for known incomplete pieces and follow-ups in `augur/sim`
and the layers immediately around it (product translator, wire, frontend).
Anything fully shipped is removed — git history is the record of done work.

## Architecture / cutover

- Replace the temporary sim dataframe-to-legacy-table materializer with
  serialized `ProjectionRun` read models. The product metric-fan path
  already bypasses polars (`monthly_metric_arrays` returns numpy direct
  from `dense.buffers.*`), but the rollout-detail endpoint still calls
  `dense.decode()` to materialize event-log polars frames before the
  product layer projects them. The durable API should expose compact
  scenario metadata + distribution-first projections instead of the
  legacy `SimulationRun` shape entirely.
- Make liquidity policies an account-keyed simulator program internally.
  Config/wire shape can stay list-friendly, but the runtime/compiler
  should consume `{(agent_id, account_id): policy}` so "one policy per
  cash account" is encoded by data shape, not validators.
- Define the month-0 anchoring rule for every model-driven level series
  in one place. Implementations exist (sp500, crypto, home_value, rent,
  PE, inflation), but no single document says whether a configured value
  is a sim-month-0 level or a fixed contract value.
- **Arrays reconcile to ledger.** Monthly result columns should remain
  charts, not truth. Keep shrinking bespoke explanatory array math
  without changing monthly-column semantics:
  - True state snapshots (cash, public asset value, private-equity mark,
    tender-eligible PE value, property value, mortgage balance,
    home-equity claims, ownership pct, net-worth metrics) sourced from
    state snapshots rather than transaction ledger rows.
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

- **Multi-scenario comparison.** The product UI today renders one
  `ScenarioKey` at a time. The deleted scenario-set surface was the only
  place where side-by-side fan-chart overlay existed (e.g. "rent for two
  years and then buy" vs. "buy now"). Re-add in the product shape:
  - URL state encodes a set (rename `?s=` → `?scenarios=`; one-scenario
    URLs decode as a 1-element set so links from before the change keep
    working).
  - One shared exogenous bundle per request (single sample of the
    market path) so the comparison stays apples-to-apples across
    scenarios — vary only the user-driven knobs, not the underlying
    paths.
  - Frontend renders matched percentile fans with one color per scenario,
    a shared x-axis, a small comparison legend, and a per-scenario
    "controls" panel collapsible into a side-by-side grid.
  - Terminal-percentile table grows columns per scenario; rollout
    sliver/event panel selection scopes to one scenario at a time.

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

## Real-estate lifecycle

The product surface handles month-0 property purchase, mortgage origination
(180/360-term fixed-rate), property tax, HOA, insurance, maintenance, MID,
and SALT. Still missing:

- **Landlord rental income** (whole-property or fractional). The user
  collects rent from a property they own, indexed to the property's
  location's rent-cost series the same way `monthly_rent_usd`
  (outside-rent) is today (`base_rent * rent_cost_series[t] /
rent_cost_series[0]`). Knobs on `PropertyPurchase` or a sibling
  `RentalIncomePlan`: `monthly_rent_collected_usd`, `fraction_rented`
  (1.0 = whole property; <1.0 = rooms / ADU / partial year),
  `vacancy_pct`, `management_fee_pct`, `leasing_fee_pct`. Sim adds a
  recurring inbound obligation from a tenant counterparty agent. The
  scenario-set surface had this; product has not yet.
- **Mid-horizon property lifecycle events** for owned property. A typed
  event timeline on `PropertyPurchase` (or a parallel
  `PropertyTimeline`) lets the user model role changes during the
  horizon. Events:
  - **Move into the property in year N** (off → owner-occupied;
    triggers §163(h)(3) MID eligibility, §121 clock start, ends the
    outside-rent obligation if applicable).
  - **Move out of the property in year N** (owner-occupied → not
    owner-occupied; opposite transitions).
  - **Start renting it out in year N** (off / owner-occupied → rental;
    enables landlord rental income from the item above; triggers
    depreciation basis allocation when supported).
  - **Stop renting in year N** (rental → off / owner-occupied).
  - **Mid-horizon property purchase** in year N (today the product knob
    is locked to "buy at month 0 or don't"; same `PropertyPurchase`
    plumbed to fire at a configurable month).
  - **Property sale** in year N or at end-of-horizon. Needs
    closing-cost schedule, mortgage payoff, §121 primary-residence
    exclusion, §1250 unrecaptured-depreciation recapture on rentals,
    and proceeds split when partner stakes exist.
    Engine support: occupancy / rental-status state machines and the
    MID/§121/depreciation rules that respect them.
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

PE tender regime is wired end-to-end (sim engine → wire → translator →
frontend LNW-floor knob). Still missing:

- **Public-market PE regime** (post-IPO unrestricted shares behaving like
  public stock).
- **Acquisition regime** (PE position bought out at a specified or
  modeled price, often above marketed valuation, with lockup variations).
- **PE IPO and PE acquisition lifecycle events** — should lower into the
  appropriate regime transition.
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
  not infer `sp500`, home-value, rent, and inflation values from
  source-specific `latest_observations` maps. The current branchy
  `_latest_factor_value` (with the recent `crypto:*` addition) is the
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
  richer assertions on event streams would catch regressions earlier.

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
