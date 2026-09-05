# Augur Rust simulator prototype

This directory contains a clean-sheet deterministic simulator that is being
built in parallel with `finance/augur/sim`. It is not yet a replacement for the
existing engine.

## Invariants

- Money is always a checked `i64` count of the fixture's declared currency
  quantum. Products use `i128` intermediates and explicit half-away-from-zero
  rounding.
- Every monetary change is a balanced compound journal entry. Entries are
  validated and applied atomically; signed debits sum to zero.
- Exogenous paths are materialized once into a strict integer fixture. Rust and
  Python/JAX consume the same fixture bytes; Rust does not resample paths.
- Independent rollouts execute in parallel with Rayon and are collected by
  deterministic rollout index.
- Obligations sharing one payer/source account settle all-or-none, matching the
  existing simulator's hard-demand grouping.
- Failed rollouts stop executing future actions and expose zero value-bearing
  snapshots while retaining the preceding causal trace.
- Full forensic output and compact population output use the same state-machine
  implementation. The compact path does not allocate every monthly snapshot or
  journal and is suitable for 100,000-rollout workloads.

`simulate_dense(...)` is the Python/JAX compatibility-output path: it retains
every monthly state snapshot and every event record behind the canonical
frames, but deliberately omits Rust's additional balanced journal
because Python/JAX has no matching output channel. `simulate(...)` remains the
strictly larger forensic path with that journal, while
`simulate_summaries(...)` retains only fixed-size terminal summaries. Dense
performance comparisons must use `simulate_dense(...)`, not the compact path.

## Covered behavior

The differential suite currently proves exact integer agreement for:

- opening balances and opening equity;
- scheduled and recurring transfers;
- scalar, tagged-fixed, and inflation/rent-series-indexed amounts across
  transfers, property cashflows, and obligations, including rollout-specific
  monthly or periodic reset boundaries and exact half-up ratio scaling;
- initial tax lots and FIFO scheduled sales;
- monthly security distributions based on currently held units, including
  independently rounded issuer tax-character slices for Treasury, municipal,
  corporate, and mixed funds;
- par-only held-to-maturity nominal bonds and TIPS, including finite coupon
  schedules, par redemption, CPI-indexed principal, deflation-floor redemption,
  phantom accretion income, and federal/state/own-issue interest exemptions;
- financed or cash property purchases with explicit property, mortgage,
  receivable, and counterparty ledger postings;
- property-gated scheduled and recurring cashflows, including ordinary-income
  and deductible-expense tax tagging;
- property sales driven by rollout-scoped home-value paths, including seller
  closing costs, mortgage payoff, realized long-term gain, lifecycle ordering,
  and same-month suppression of property-tied cashflows and carrying costs;
- initial and mid-horizon primary-residence assignment, sale-driven assignment
  clearing, the exact 24-of-trailing-60-month use test, and filing-profile §121
  exclusion caps applied after depreciation recapture;
- property rented-fraction transitions, capital improvements, 27.5-year rental
  depreciation, rental/owner mortgage-interest splitting, per-jurisdiction
  acquisition-debt principal caps, home-equity-debt exclusion, and sale-time
  §1250 recapture with federal capped-rate versus state ordinary-income
  treatment;
- fixed-payment mortgage origination, monthly interest/principal splitting,
  same-source funding-group settlement, and property-tax carrying costs;
- grouped scheduled and recurring obligations, including property-gated ones
  that stop accruing at the sale and deduct their property's runtime rented
  share of every payment from the payer's ordinary income;
- target-allocation cash-band raises before obligation funding, including
  projected end-of-month demand, exact integer water-filling, source-account
  order, FIFO lot dispositions, immutable sleeve weights, realized gains,
  attempted-funding attribution, and canonical obligation-failure metadata;
- private-equity protocol execution after settlement, including typed issuer
  marks/regimes/events, tender capacity and eligibility, liquidity blocks,
  public-market floor sales, forced-sale fractions, forced-recovery cashouts,
  deterministic issuer/FIFO order, liquid-net-worth floors that exclude PE,
  canonical opportunity traces, lot dispositions, and capital-gain effects;
- reduced-form tax-loss harvesting after settlement, including the calibrated
  maturity/drawdown curve, exact PPB parameter transport, short/long loss
  allocation, adjusted-basis harvest ceilings, persistent cumulative deferral,
  and proportional give-back through scheduled and target-allocation sales;
- insufficient-cash failure month and state freezing;
- federal and California ordinary-income year-end tax accruals;
- federal SALT itemization from funded property tax plus sibling state-income
  tax, including forward-filled year-indexed cap schedules;
- federal long-term-capital-gain stacking and tax accrual;
- quarterly estimated-tax payments, aggregate safe-harbor Q4 computation,
  January true-up, tax-liability settlement, and funded/unfunded tax-payment
  events;
- generated benchmark fixtures at 17 rollouts.

The Rust ledger also records tax expense/liability accrual entries, tax
prepayments and settlement, and nets capital gains/losses once per taxpayer so
one shared ordinary-loss offset and carryforward feed every jurisdiction in
later tax years. Monthly and terminal output retain jurisdiction-level tax-liability
state and held bond principal; selected traces expose tax-payment,
tax-settlement, and issuer-attributed bond cashflow/accretion records.
Monthly snapshots also retain taxpayer capital-gain state and each TLH policy's
cumulative harvested-loss ledger; compact terminal summaries preserve the TLH
ledger because it is future adjusted-basis state rather than explanatory trace.
`event_frames.rs` emits every canonical `EventLog` frame directly, in Augur's
own column names and units: `Money` becomes a `_quanta` column, a rate in parts
per billion becomes the fraction Augur reports, and a `Quantity` divides by its
lot's scale. The knowledge of those units therefore lives beside the engine that
defines them, and a field renamed here fails the Rust build rather than turning
up later as a missing key in a Python decoder. `event_log.py` only checks
an arriving document frame-for-frame and column-for-column against the schemas
in `sim/events.py`. Event frames are an explanatory-output boundary; snapshots
remain authoritative state and events are not replayed to reconstruct them.

The strict fixture stores monetary series (security prices, distributions, and
home values) as currency quanta. Inflation and rent index levels instead use a
dimensionless parts-per-billion scale. Referenced index levels must be positive
and must round-trip exactly through the Python/JAX `float64` external-series
boundary; the Rust validator and Python adapter reject fixtures that would lose
an integer level during that conversion. Series coverage is deliberately dense:
every series supplies every rollout and snapshot in the fixture.

Private-equity channels use the same dense row-major fixture contract but keep
their distinct types explicit in the series names: mark/recovery/valuation are
currency quanta, capacity/eligibility/forced-sale fractions are exact PPB,
regime/event-kind are validated integer codes, and opportunity/blocked channels
are 0/1. The Python adapter reconstructs the typed `PrivateEquityBundle` only at
the legacy boundary; Rust never routes PE marks through ordinary security-price
series.

TLH policies encode every heuristic parameter as integer PPB. Rust evaluates
the same float64 maturity/drawdown curve as JAX, quantizes the resulting monthly
factor back to PPB before applying it to integer money, and keeps the give-back
ledger entirely in currency quanta. The shared differential fixtures cover
drawdown versus flat paths, year-end tax facts, two-stage partial liquidation,
and target-allocation sale give-back.

Initial lots store total basis, but that total must imply an exact
integer-currency-quantum basis per whole unit:
`basis × quantity_scale` must divide evenly by `units`. The Rust validator and
`fixture_encoder` both refuse an inexact lot rather than letting one engine hold
the per-unit basis and the other a floored total.

Bond coupon rates use the same parts-per-billion contract and must round-trip
exactly through the legacy Python/JAX `float64` boundary. Nominal coupons round
the full `face × annual rate × period / 12` rational once; TIPS preserve the
legacy engine's indexed-principal and fixed-point period-rate path. Government
issuer levels come from one scenario-level jurisdiction identity registry,
rather than duplicated caller-supplied metadata on each bond.

Distribution tax-character fractions use exact PPB weights that must be
positive, sum to one, and preserve the same issuer identity contract as bond
interest. Each slice is paid, journaled, attributed, and routed through the
jurisdiction's interest-exemption policy independently; the slice sum is the
fund's cash payout.

Target allocation evaluates the band after all monthly obligations have
accrued, sells before the grouped funding check, and therefore makes an unpaid
obligation mean the configured portfolio genuinely could not fund it. Buy
orders are decided from that same pre-settlement observation but execute only
after obligations settle, with a floor affordability clamp against then-current
cash. Each purchase fills the next preallocated per-sleeve lot slot, records the
rollout's observed price and acquisition month, and aborts the run rather than
silently dropping a purchase when capacity is exhausted. Purchased lots join
the first configured source-account pool for later FIFO sales. Selected traces
expose the source and proceeds accounts on every lot disposition and the ordered
sleeve identities attempted for every matching obligation. Optional quiet-band
drift rebalancing is all-or-nothing, returns every sleeve to its floored target,
and suppresses itself whenever the cash band is already raising or investing.
Sleeve quantity scales are explicit fixture integers, taken by `fixture_encoder`
from the sleeve's own asset, so the fixture cannot state a scale the Python side
does not use.

## Product read model

`simulate_product_metrics(fixture, primary_agent_id)` emits the seven base product metric
series plus the per-rollout failure month, under the compact capture mode — no monthly
snapshot, journal, or event trace. `backend.py` wraps it as the product API's
`ProductMetricArrays` and `ProductProjectionSummaries`, composing the derived metrics and
the percentile fan with `product/metric_composition.py` and `product/quantiles.py`, the
same code the JAX backend runs. Design, and the two JAX behaviours the Rust engine matches
deliberately rather than corrects: [docs/product_metrics.md](docs/product_metrics.md).

`fixture_encoder.py` is what connects that to a live request: a `Scenario` and its
`CompiledSimulation` become the strict integer fixture, taking money straight out of
`external_money_values` and quantizing index levels to the same parts per billion JAX rounds
them to before any multiplication.

Which engine serves is `Config.simulation_backend`, defaulting to JAX. It covers all four
projection endpoints, the selected rollout included: `ProductService` holds an `Engine`
(<../sim/backend.py>) rather than branching per call site, and everything above that contract
is written once — the derived metrics, the terminal reduction, the percentile brackets, and
the rollout projection, which reads the canonical event frames both engines emit rather than
either one's output layout. So a Rust answer equals a JAX answer by construction.

Two differential suites hold that: `product_scenario_test.py` answers one `ScenarioKey`
against the fixture deployment's own portfolio and sampled model identically on both
backends, funded and after ruin; `rollout_projection_test.py` renders one selected rollout's
causal trace from each engine's frames and compares them.

A scenario the fixture cannot express is refused rather than encoded without it. The live
case is a purchased property: its recurring HOA, insurance and maintenance obligations carry
a Schedule E deduction category and a property gate, and `ObligationSpec` has neither field.

Why the fixture is a third model beside `Scenario` and `CompiledSimulation`, what the schema
being written on both sides does and does not expose, and the intended end state:
[docs/fixture_boundary.md](docs/fixture_boundary.md).

## Python extension

`simulator.so` is a `rust_shared_library` built from `python.rs`, imported as
`finance.augur.rust.simulator` and typed by the hand-written `simulator.pyi`. Fixtures
cross as JSON text because that is the simulator's input contract; results cross as Python
integers, so the fan workload never pays for a dense JSON round trip. `simulator_cli`
remains for out-of-process forensic runs.

Still missing before replacement is plausible:

- broader modeled tax facts and complete deduction policy;
- mortgage contracts beyond the basic fixed-rate purchase mortgage;
- property-tax policy beyond purchase-price assessment and fixed location
  special assessments;
- broader liquidity policy;
- complete selected-rollout causal trace parity for those domains — the projection reads both
  engines now, so what is left is the domains themselves rather than the plumbing.

What `fixture_encoder` still refuses, and how much of it a real request can reach, is a
different list and a shorter one:
[plans/rust_as_default.md](plans/rust_as_default.md) § Coverage.

## Layout

`engine.rs` is the orchestrator: the rollout month loop, the public entry points, and the
shared per-rollout state. Each policy family it drives lives in `engine/` beside it —
`validation`, `property`, `obligations`, `taxes`, `securities`, `target_allocation`,
`private_equity`, `tlh`, `cashflows`, `recorder`, `accounts`, `errors`. Submodules reach
the shared state through `use super::*`, and expose to the root only what it calls;
anything a module uses alone stays private to it, which the single 7.5k-line file could
not express.

The Rust/JAX differential harness and its suites live in `differential/`, one suite per
policy family; the Rust half of the throughput benchmark lives in `benchmark/`. The
feature-rich scenario both measure is not Rust's and lives in
<../benchmark/scenario.py>; `benchmark/fixture.py` here only writes it out as the integer
document, which the standalone binary needs on disk and the in-process bindings do not.

## Targets

```text
//finance/augur/rust:simulator_cli
//finance/augur/rust:simulator_ext
//finance/augur/rust:simulator_test
//finance/augur/rust/differential:all
//finance/augur/rust/benchmark:all
//finance/augur/rust/benchmark:fixture_bin
//finance/augur/rust/benchmark:driver_bin
```

`simulator_cli FIXTURE.json OUTPUT.json` retains full forensic traces. The Rust
benchmark driver's default `--output-mode dense` retains monthly state and
compatibility events; `--output-mode compact` selects the older terminal-summary
throughput workload. See [benchmark/README.md](benchmark/README.md) for the
measured baselines and their output-contract caveats.
