# Augur Unified Plan

Last consolidated: 2026-05-20.

This plan consolidates the active Augur work from the public framework docs and
the private deployment notes. It is the priority ordering. `augur/TODO.md`
remains the detailed public backlog; private values, holdings, property data,
and deployment-specific composition stay in the downstream private repo.

## Sources

- `augur/SPEC.md`: product contract and simulator vocabulary.
- `augur/sim/README.md`: sim purpose, boundaries, invariants, and rollout
  failure semantics.
- `augur/sim/REQUIREMENTS.md` + `augur/sim/DESIGN.md`: simulator capability
  surface and structural decisions.
- `augur/plans/e2e_redesign.md`: ledger/reconciliation work for monthly
  result arrays.
- `augur/sim/docs/tensorized_simulator.md`: rollout-axis tensorization design + invariants.
- `augur/sim/docs/tax_engine_evaluation.md`: tax engine build-vs-adopt evaluation.
- `augur/docs/prior_art_audit.md`: external architecture lessons for path
  identity, governance, policy projection, and accounting traces.
- `augur/sim/TODO.md`: forward-looking sim/product follow-ups.
- `augur/TODO.md`: public generic backlog.
- `gaffer-private/TODO.md`: private personal-finance modeling follow-ups.
- `gaffer-private/x/augur/SPEC.md`: private deployment boundary and image
  privacy contract.

## North Star

Augur simulates one (and, on the roadmap, a small set of) `ScenarioKey`s across
sampled exogenous paths and returns a distribution over trajectories. A
selected rollout is an inspection aid, not a separate deterministic product.
The UI, app state, and result APIs should make that distinction impossible to
miss.

The core model should stay structured around actors, accounts, assets,
liabilities, markets, policies, actions, ledgers, accounting detail, and
balance snapshots. The app may provide friendly controls, but it should not use
a flat browser-side "scenario row" as the source of truth and then expand it
back into typed backend objects.

The intended production backend path is `augur/model -> augur/sim -> augur/api`:
model providers sample exogenous levels/events with provenance, `augur/sim`
deterministically evaluates typed scenarios over those paths, and `augur/api`
serves compact projection/read models. The backend now executes only this sim
path. The current compatibility response proves browser-shaped smoke requests
can sample a shared market bundle, run `augur/sim`, and derive
graphable response tables from sim dataframes; the next cutover work is
broadening that slice and replacing the temporary legacy-table materializer
with final read models.

Near-term translation order:

- Fix current smoke-slice correctness first by moving opening public
  securities into backend YAML config as actual positions: position id, account,
  owner, symbol/security identity, exogenous-model series mapping, units, starting
  price, and cost basis. The sim translator should create concrete initial lots
  and derive current value from `units * price[t=0]`, not interpret a scenario
  `value_usd` as quantity.
- Then add tax profile/ordinary income translation, outside-rent obligations,
  and the current dataframe-derived response tables. **Ordinary income
  translation is currently deferred (low priority, 2026-05-24):** the
  scenarios we plan around today are post-earning retirement projections
  rather than active W-2 income, so the income knob is not on the near-term
  path. Outside rent and the response shape work continue as planned.
  Outside rent can start as
  a flat compatibility obligation, but the target is indexing it to modeled
  rent-cost series from `augur/model` for the applicable rental market: configure
  current rent on the as-of date plus a model series key, then scale future rent
  by the series ratio from that as-of value. Generalize this into a
  path-indexed amount contract for other recurring cashflows instead of adding
  one-off inflation/rent flags.
- Continue the first property slice: month-0 purchase and mortgage origination
  smoke through the backend, then add property tax. Keep this slice
  narrow: purchase is month 0, occupancy is forever when selected, rental state
  does not transition mid-horizon, and any sale support can be end-of-horizon
  only if needed for graphs. Property value should start from the
  configured/list value and index by the modeled home-value series; future-month
  purchase semantics are deferred. When native rental cashflows land, tenant
  rent income for owned properties should use the same current-rent plus modeled
  rent-cost series indexing contract as outside rent.
- Add crypto positions and liquidity preferences after the property smoke is
  graphable.
- Add private equity, tender/public/acquisition regimes, and partner property
  stakes after those concepts have native sim state and event streams.
- Once the compatibility slice is broad enough for the frontend, replace it
  with a native sim request schema or keep it only for legacy imports.
- Keep backend/sim accounting in nominal dollars through the cutover. Any
  inflation-adjusted display belongs in a later postprocessing/read-model layer.
- Derive bootstrap/UI defaults from YAML deployment config and remove or hide UI
  toggles for facts that should remain config-only, especially initial
  positions.
- Split counterparties and accounts as the relevant cashflows land: landlord,
  tenant, lender, seller, tax authority, HOA, insurer, brokerage, crypto
  exchange, and other bookkeeping identities should be explicit rather than
  collapsing into one generic external sink.

## Prior-Art Shape For Core Cleanup

The prior-art audit points to a conservative target shape:

- Exogenous path generation and household projection stay separate.
  `ExogenousSamplingRequest` plus `SampledExogenousBundle` is the durable economic
  scenario-generator boundary; the simulator is deterministic once it receives
  a typed scenario and sampled exogenous paths.
- Trajectory identity includes scenario input, exogenous model identity,
  evidence/calibration identity, generator implementation/version, seed, path
  index, and any non-exogenous event streams. `rollout_index` remains a convenient
  selector, not a reproducibility key by itself.
- Actor policy programs are ordered programs. Policy steps emit decisions and
  instructions; accounting/runtime code validates and applies effects. New
  policy families should not reintroduce per-class execution loops.
- Ledgers, balance snapshots, accounting detail, lots, liabilities, and typed
  cause IDs are the source of truth. Monthly arrays are chart/report views over
  that state, not a parallel semantic model.
- Model governance is part of the model output. Sampled bundles now carry first
  typed model/evidence/calibration/generator/path identities; the next cleanup
  is to persist real artifacts and validation results behind those IDs.

## Priority 1 — superseded: typed result views

This priority described the typed result-panel contract
(`data-result-panel-kind` = `distribution` / `trajectory` /
`accounting_detail`) and the `/inputs` persistent edit surface that
lived in the deleted scenario_set frontend. The product surface uses
a much narrower UI shape and a single `MetricFanResponse` +
`RolloutResponse` API; result-panel typing is no longer load-bearing.

Surviving guardrail: Mantine remains the standard React component
kit. `MantineProvider` wraps the product shell in `app.jsx`; keep
migrating remaining controls to Mantine rather than inventing local
widgets. The deferred multi-scenario comparison feature
(`augur/sim/TODO.md` "Product UX") will need a comparable typed shape
when it lands.

## Priority 2 — done: scenario_set browser state retired

The whole scenario_set frontend (its hand-written URL state encoder,
section overrides, app shell) was deleted in favor of the much narrower
`ScenarioKey` payload that the product surface consumes. Multi-scenario
comparison is the only behavioral gap from that surface that survives
as a forward-looking item; see `augur/sim/TODO.md` "Product UX".

## Priority 3 — done: sampled PE / tender timing / crypto

All three variance sources are now sampled, not flat:

- **PE valuation** is sampled per-issuer via `PreSampledPrivateEquitySampler`
  consuming a JSONL trajectory artifact from the gaffer-private
  joint-fit pipeline (5-15 historical tenders → posterior over price ×
  timing). The product wire exposes `pe_tender_policy` (LNW floor +
  optional inflation indexing) and the sim engine drains lots FIFO at
  each tender event.
- **Tender timing** rides in the same JSONL artifact as sampled
  `private_equity_sale_opportunity:<issuer>` event streams. Two
  rollouts of the same scenario now see different tender months.
- **Crypto** flows as `crypto:<symbol>` factors through the VECM joint
  fit (BTC + ETH wired in `evidence_data.py`; `_latest_factor_value`
  resolves crypto:btc → `btc_close_latest` etc.). The calibrated blob
  at `augur/fit/calibrated/trained_vecm.npz` includes their
  posteriors. Crypto holdings flow through `portfolio.holdings` as
  `cryptocurrency` `security_kind` and are sellable through the
  liquidity `crypto` bucket.

Remaining open follow-ups in this area:

- **PE public-market and acquisition regimes** (post-IPO unrestricted
  shares; acquisition buyout). Tracked in `augur/sim/TODO.md`
  "Private equity".
- **Mortgage-rate path sampling** (today: single PMMS survey number;
  no `mortgage30:*` series in the VECM). Tracked in `augur/sim/TODO.md`
  "Exogenous sampling / VECM".

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
- Keep exogenous paths and opportunities as observations, not policy
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

## Next Lanes (parallelism + sequencing)

- **Tax surface beyond sale tax** — qualified dividends, short-term gains,
  capital losses + carryforward, rental income tax, passive-loss release.
  (~~SALT/property-tax federal deduction~~ done — `FederalSaltDeductionPolicy`
  with year-keyed cap; AGI phase-out + sales-tax election still deferred.)
- **`RegimeChange` mid-rollout events** — IPO converts
  `LiquidityEventOnly` → `PublicMarket`. The discriminated-union shape
  already supports it; runtime needs to sample the event month and flip
  the variant. Companion to PE acquisition events.
- **Mortgage-rate path sampling** — today the mortgage rate is a single
  PMMS survey number at scenario time; required-series introspection
  doesn't cover a `mortgage30:*` path. Adding it would let "what if
  rates fall to 5% in 18 months" scenarios work.
- **Underpayment penalty on quarterly estimates** — IRS interest rate +
  3% on shortfalls. Layers on the year-end true-up already in place.
- **Borrowing facilities** — overdraft, margin, credit line as explicit
  funding sources in the obligation pipeline. Today negative cash is a
  silent warning; with explicit borrowing it becomes an
  accounting-tracked liability paired with a funding source.
- **Persist model-governance artifacts** — durable evidence / calibration
  / validation-report storage for market providers. `augur/model/`.
  Self-contained, can run in parallel with anything.
- **Reintroduce partner/co-owner agreements** after sim has a tested
  agreement model. "Agent X pays agent Y this amount over this period
  for this share/claim" should come back as a tested agreement model
  in `augur/sim`, not as a scenario-wide enum.

## Next Work Plans

### Plan D: Keep Evidence Configuration Typed At The Boundary (guardrail)

Keep the exogenous evidence config Pydantic-parsed at load time, with
`evidence_config_test` as the review point when adding new source-data
fields or deployment-supplied config. Reject stale simulation knobs at
the file boundary — `SamplingRequest` owns rollout count, horizon, and
seed; the market config should not keep a second inert copy.

## Verification Loop

For each public framework slice:

```bash
bbr test //augur:browser_shell_test
bbr test //augur:visual_test
bbr test //augur/api:server_test
```

Before handing off a broader spiral:

```bash
bbr test //augur/...
bbr build //augur/...
```

For private deployment slices, also run the private Augur browser/backend tests
and verify the live deployment only after the public framework commit is
repinned downstream.
