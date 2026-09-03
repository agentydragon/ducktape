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
every monthly state snapshot and every event record consumed by
`output_adapter.py`, but deliberately omits Rust's additional balanced journal
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
- grouped scheduled and recurring obligations;
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
`output_adapter.py` lifts integer-native Rust transfer, lot-disposition,
obligation-accrual, obligation-settlement, and rollout-failure rows into the
same canonical `EventLog` frame schemas exposed by Python/JAX. It also
normalizes the currently supported property-purchase, mortgage-origination,
mortgage-payment, rented-fraction, capital-improvement, and property-sale
frames, primary-residence assignment frames, year-end tax accrual/breakdown
and tax-liability settlement frames, plus private-equity protocol and
opportunity frames, including exact frame dtypes and cause identities. The
adapter is an explanatory-output boundary only; snapshots remain authoritative
state and events are not replayed to reconstruct them.

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
`basis × quantity_scale` must divide evenly by `units`. Both the Rust validator
and Python adapter reject an inexact lot rather than letting the legacy adapter
floor a different basis.

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
Sleeve quantity scales are explicit fixture integers and the adapter verifies
that each one matches the canonical Python asset scale before differential
execution.

Still missing before replacement is plausible:

- broader modeled tax facts and complete deduction policy;
- mortgage contracts beyond the basic fixed-rate purchase mortgage;
- property-tax policy beyond purchase-price assessment and fixed location
  special assessments;
- broader liquidity policy;
- complete selected-rollout causal trace parity for those domains;
- Python extension/Arrow output integration.

## Targets

```text
//finance/augur/rust:simulator_cli
//finance/augur/rust:simulator_test
//finance/augur/rust:differential_test
//finance/augur/rust:benchmark_fixture
//finance/augur/rust:benchmark_driver
//finance/augur/rust:jax_benchmark_driver
```

`simulator_cli FIXTURE.json OUTPUT.json` retains full forensic traces. The Rust
benchmark driver's default `--output-mode dense` retains monthly state and
compatibility events; `--output-mode compact` selects the older terminal-summary
throughput workload. See [BENCHMARK.md](BENCHMARK.md) for the measured baselines
and their output-contract caveats.
