# Spike 1

End-to-end benchable scenario that exercises the parts that historically
got tangled in the legacy engine — market-driven rollout divergence,
the lot model + tax classification, federal + CA year-tax math with
quarterly + year-end timing, the obligation funding chain. Validates
the event-log-canonical / polars-vectorized architecture at scale.

See <REQUIREMENTS.md> for the full target spec and <DESIGN.md> for
the structural plan.

## In scope (REQUIREMENTS layer coverage)

- **L1-L3** — transfers, time, rollouts. The spine.
- **L4.1-4.7** — lots, FIFO cost basis (FIFO only this spike; HIFO /
  specific-id / average deferred), sale crossing lots with mixed
  holding periods.
- **L4.8** — many positions through one code path. The DRY proof
  point.
- **L5** — state-dependent decisions over the rollout column.
- **L6.1-6.4** — LTCG/STCG classification, year-tax accrual,
  quarterly estimated + year-end true-up. (NIIT deferred.)
- **L7.1, 7.2, 7.4, 7.5** — federal + CA brackets, single filer,
  standard deduction, combined ordinary + capital. (Itemized
  deductions deferred — nothing to itemize against until L8 / L12.)
- **L9.1, 9.2, 9.6** — floor-triggered sale, asset preference chain,
  fixed monthly spend.
- **L10.1-10.3** — market model integration (per-rollout sampled
  paths, agent decisions don't feed the market, agents share path
  within a rollout). Sellability mask plumbed but no scenarios
  exercise it.
- **L11.1, 11.2** — cash-negative failure flag, per-rollout failure
  scope. (Recovery semantics deferred — failed rollouts stay failed
  this spike.)

## Deferred to later spikes

L8 mortgages • L12-13 housing (purchase / sale / depreciation / §121
/ §1250 / occupancy / multi-property) • L14 constrained sellability
(mask wired but no scenarios) • partner-equity stretch • S9.7
variable spend from market • HoH filing status • NIIT • itemized
deductions • S11.3 failure recovery • multi-jurisdiction beyond
federal + CA • multiple Bay Area locations beyond `san_francisco`.

## Deliverable: benchable scenario

One tax-paying agent (Alice, single filer, located `"san_francisco"`):

- Configured W-2 ordinary income of \$200k/year, arriving monthly.
- Three capital-gains-eligible positions — `"vti"`, `"qqq"`, `"btc"`
  — each with initial lots configured at scenario start.
- Market bundle: per-rollout per-month price multipliers for each
  position (simple stochastic model, GBM-like or bootstrapped from
  a configured distribution; doesn't need to be the production
  market model yet).
- One floor-triggered sale policy: "if checking < \$5000, sell
  \$20000 of vti, then qqq, then btc in order".
- One \$5000/month recurring spend obligation.
- Federal + CA year-tax computation with quarterly estimated
  payments and year-end true-up; prior-year-tax knob supplied via
  scenario.
- Horizon: 5 years (60 months) × 1000 rollouts.

## Success criteria

1. **Architecture proven.** The event log is canonical;
   `apply_events` is the single state-mutation point; every phase
   of `step_emit_events` is polars expressions over the rollout
   column with no Python rollout loop.
2. **Replay invariant holds.** For every M,
   `state_at(M).event_sourced == apply_events(initial,
log.filter(month ≤ M))`. Asserted in tests; opt-in
   `--check-replay` flag for production.
3. **Tax math correctness.** The deterministic-market single-
   rollout case computes federal + CA year tax to within 1¢ of a
   hand-computed expected value. The quarterly + year-end true-up
   payments sum to the full-year tax exactly.
4. **Rollout divergence.** 1000 rollouts with the same scenario
   produce 1000 different end-state net-worth trajectories because
   the market paths differ. Same market paths with different
   policies also diverge.
5. **Benchable.** A bench script (sibling to
   `augur/core/bench_augur_run.py`) runs the representative
   scenario end-to-end on RBE and reports wall-clock time. No
   specific perf target this round — establishes the baseline that
   later waves' bench targets are set against.
6. **DRY proof.** Adding a 4th capital-gains-eligible position to
   the scenario is a config edit (one Position record + one market
   path); zero engine code touched. A test exercises exactly this.

## What spike 1 intentionally does NOT prove

- That mortgages, property purchases / sales, depreciation, §121,
  §1250 work — those require L8 + L12-13 templates, deferred to
  spike 2.
- That the wire schema matches existing `ScenarioRunArrays` —
  deferred. The new sim has its own `SimulationRun` output type
  for now; the legacy-wire adapter is a separate concern.
- That partner-equity multi-stakeholder works — deferred.
- That performance beats the legacy engine — measures it, doesn't
  yet require a specific target. Tightening comes after L8 + L12
  land and we can compare apples to apples.

## Implementation order (≈15 commits)

Each commit stands alone with adjacent tests. Earlier commits don't
build later-commit infrastructure.

1. **L1.1-1.3** — `cash_balances` frame schema, `Transfer` event,
   `apply_events` skeleton dispatching on event kind, `simulate()`
   loop with one rollout. Alice gives Bob \$5 test.
2. **L2.1-2.2** — multi-month loop, recurring transfer (paycheck
   arriving each month as repeated Transfer events).
3. **L3.1-3.3** — rollout dimension scales from 1 to N as a polars
   column. Multi-rollout tests; assert at 1k rollouts.
4. **L4 lots part A** — `asset_lots` schema, `AssetPurchase` +
   `AssetSale` events with FIFO lot consumption, lot-disposition
   projection. Single-lot scenarios.
5. **L4 lots part B** — multi-lot scenarios (S4.4-S4.6): mixed
   holding periods, sale crossing two lots, per-lot LTCG/STCG
   classification.
6. **L5 + L10 market integration** — `MarketBundle` interface,
   per-rollout per-month price paths, asset market values flowing
   as market-derived state attributes, sellability_mask plumbing
   (no scenarios exercise non-default mask yet).
7. **Jurisdiction YAML loader** —
   `data/jurisdictions/{federal_us,california}.yaml`, Pydantic
   models, validation, loader.
8. **L7 ordinary income tax** — `tax_liability` template;
   ordinary-income year-tax computation for federal + CA single
   filer + std deduction; year-end tax accrual event creates a
   `tax_payable` liability.
9. **L6 capital gains tax** — LTCG/STCG classification function
   over the lot_dispositions projection; year-tax including LTCG
   bracket walk; integrate with ordinary year-tax.
10. **L6 quarterly + year-end timing** — quarterly markers (Apr 15,
    Jun 15, Sep 15, Jan 15 of next year), safe-harbor with
    `prior_year_tax_usd` scenario knob, year-end true-up, tax-
    payment events.
11. **L9 + L11 policies + failures** — floor-triggered sale, asset
    preference chain, monthly-spend obligation, obligation funding
    chain (sell to cover insufficient cash), failure-event
    emission, rollout-status flip.
12. **Locations YAML loader** —
    `data/locations/san_francisco.yaml` (the only one for spike 1;
    no property templates yet). Wired to agent's residence.
13. **End-to-end bench scenario** — the representative scenario
    described above; bench script; capture baseline timing on RBE.
14. **DRY test + cleanup** — the "add 4th position by config" test;
    tighten any rough edges; doc/comments pass.
