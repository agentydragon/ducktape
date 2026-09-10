# Augur simulator

The deterministic engine behind every Augur projection. It runs a compiled plan
from `finance/augur/sim` (which holds the scenario model and the engine
contract, and no engine) and answers in canonical event frames and state
channels.

`execution.rs` declares the production `ExecutionInput`, runtime state, and output
records. `ValidatedInput` checks an input once before any rollout executes. Test and
benchmark fixture helpers construct that same input; they are not a separate schema.

Internally, `RolloutState` initializes opening books once and advances one month at
a time. The full-horizon drivers loop over that same advancement. Completed or
failed states do not advance or invoke policies; an execution error consumes the
state, preventing continuation from a partly applied month. This is not a public
actor-session API. Existing spending controls review opening-month
holdings; the separate scoped action control below reviews assembled monthly claims.

The existing extension also exposes `PrototypeSpendingSession`: an experimental
Python-controlled `observe()` → decision → `advance(requests)` monthly handoff.
It owns one compiled input and retained books, releases the GIL for native work,
and uses the same evaluator as the full-horizon spending driver. Its copied integer
observation columns contain original path IDs, month, actor cash/public value and
current/origin CPI, not future paths. Requests carry `(path ID, observed month,
nominal currency quanta)` and may be reordered or chunked. Policy memory belongs
to the experiment and must follow original IDs, never temporary batch positions.
Stopped paths disappear from observations; compact result columns follow the
constructor's selection order, while forensic replay retains original path IDs.
`finish_json()` consumes terminal results. Invalid requests or native errors close
the entire prototype session; insufficient funding retains the current per-path
stop behavior. Call `close()` if a Python policy raises. There is no resubmission
or within-month policy callback, and this is not the supported actor-action API.
This first transport boxes/copies integer lists and serializes final output as JSON;
it makes no zero-copy or high-N throughput claim.

## Invariants

- Money is always a checked `i64` count of the fixture's declared currency
  quantum. Products use `i128` intermediates and explicit half-away-from-zero
  rounding.
- Every monetary change is a balanced compound journal entry. Entries are
  validated and applied atomically; signed debits sum to zero.
- Exogenous paths are sampled in Python and materialized once into a strict
  integer fixture. Rust does not resample paths.
- Independent rollouts execute in parallel with Rayon and are collected by
  deterministic rollout index.
- The configured runner groups claims and chosen consumption by payer/source
  account and settles each group all-or-none. This is an explicit control, not a
  restriction imposed by individual payment execution.
- Failed rollouts stop executing future actions and preserve the actual stopped
  book and causal trace. No later forensic snapshots or events are emitted.
- Full forensic output and compact population output use the same state-machine
  implementation. The compact path does not allocate every monthly snapshot or
  journal and is suitable for 100,000-rollout workloads.

`simulate_dense(...)` retains every monthly state snapshot and every event
record behind the canonical frames, but omits the balanced journal.
`simulate(...)` is the strictly larger forensic path with that journal, while
`simulate_summaries(...)` retains only fixed-size ending-book summaries. Dense
performance comparisons must use `simulate_dense(...)`, not the compact path.

`engine::spending::simulate(...)` adds an experiment-authored spending function,
constructed separately for each rollout. It sees opening holdings at current
prices and origin-relative CPI before monthly cashflows; it returns nominal
consumption, funded alongside the execution input's other obligations. The function owns
its review cadence and memory. This entry point retains forensic output;
`engine::spending::simulate_summary(...)` returns the identified component's
`consumption_requested` and `consumption_paid` plus the payer's existing seven base
metric series and failure months, without retaining monthly snapshots, journals or
event traces. Consumption arrays are `[rollout][event month]` in input currency
quanta: live zero requests are zero, the failure month is included, and subsequent
unobserved months are absent. Product metrics retain their separate snapshot-major
layout and opening snapshot, with explicit validity for stopped paths as described
in <docs/product_metrics.md>. Actual payment comes from
that demand's receipt, even when another funding group's failure stops the path.
Other consumption (including committed rent), taxes and asset purchases are not
part of the policy component. `engine::spending::trace_rollout(...)` replays one
original path with fresh policy state and its original factory/series identity.
These entry points are native-only; Python/batched callbacks are not exposed here.

The Python <../x/allocation_glide/README.md> consumer submits explicit trades and
claim payments through `ActionSession`. Its optional <../policy/sleeves.py> helpers
choose withdrawals, deposits and drift trades over named account/asset pools;
the executor receives exact lot/quantity actions, not target weights.

## Exact trades

`engine::trades` defines `SaleRequest` (explicit lot/account/unit selections) and
`PurchaseRequest` (exact units, holding pool, cash account and new lot identity).
The month loop supplies execution prices; requests cannot choose a price or mutate
the book. Scheduled sales, allocation funding/rebalance sales and PE protocol sales
select FIFO explicitly, then use one sale operation. A caller can instead select a
newer lot without changing its proceeds, basis or tax-accounting implementation.

Execution terms distinguish per-unit quotes from a stated total recovery cashout.
Total cashouts are apportioned by selected economic units (including scale differences
between accounts), flooring each share then assigning leftover currency quanta by largest
fractional remainder, with request-order ties. PE recovery selects its remaining lots in
FIFO order. The total survives unchanged in cash and dispositions; each fully disposed
lot consumes its exact remaining basis. This convention does not promise that splitting
a lot preserves its tax attribution.

Sale preparation checks the whole request and stages only affected lot balances,
capital-gain rows and TLH entries. Journal and receipt counters are checked before
posting. Rejection therefore changes none of those books or records. This guarantee
is per trade, not a rollback of prior monthly actions or a batch of trades.

`ScenarioSpec.holding_pools` declares owner/account/asset/quantity-scale bindings,
including empty pools. These bindings, not initial lots or allocation strategies,
own purchase admission, observable public-price scope and internal asset accounts.
Every initial lot must match its declared pool; every public pool requires its supplied
price path. A holding account need not also be a declared cash account.

Purchases use already declared holding pools and exact quantity scales. Allocation
still chooses/clamps its order after funding; the exact executor rejects insufficient
cash. Cash accounts and holding pools have different declarations. New actor invocation, transfer admission,
settlement delays and alternative funding/rebalance strategies are not provided by
this module.

## Transfer accounting

`engine::transfers::TransferRequest` names the cause, source, destination and exact
amount. The execution operation has two engine-controlled admission contexts:
an actor ID requires owned, declared, funded cash; no actor ID denotes an already
scheduled cashflow. All scheduled/recurring transfers and property-gated cashflows
use this same operation after resolving their current amount.

Actor requests cannot supply tax labels. Scheduled contracts retain the existing
income-source and ordinary-deduction rules, and can debit their source below zero;
this preserves exogenous cashflows without giving actors implicit credit. The shared
posting routine stages only the affected income rows and validates the journal
counter before posting cash. Overlapping income/deduction rows use the pending value,
not the original value twice. A failed request preserves cash, tax rows and receipts.

This is a transaction primitive, not an actor invocation loop or claim-payment API.
No policy selection, automatic funding, delayed settlement or batch rollback is added.

## Scoped holdings

`holdings.rs` reads canonical books without a reporting layout. `AgentHoldings`
resolves an actor's declared cash accounts and public price rows once; spending
observations and product capture use that scope. Allocation instead selects its
funding account and declared source pools/sleeves. A borrowed `LotView` shares
per-lot valuation: round each lot to currency quanta before adding values.
Neither scope implies after-tax liquidation proceeds. Callers supply the observed
mark month; a stopped book uses its failure-event marks, never future prices.
CPI in the spending observation is current inflation relative to path origin.

`engine::observations::ActorBooks` gives spending functions borrowed account,
remaining public-lot/basis, active borrower-mortgage and recorded tax views at
their opening review. The view fixes the actor and mark month; it cannot read
another actor's books or request future path values. Tax views expose recorded
income, jurisdiction facts and assessed outstanding liabilities, not hypothetical
future assessments. Scheduled purchases are not originated contracts.
Reduced-form harvesting's cumulative basis reductions remain separate scoped
account/asset-pool facts; a public lot's `book_basis()` alone is not its complete
adjusted tax basis when harvesting is enabled.

`holding_pools()` also exposes owned public pools with no remaining lots, and
`public_price(asset_id)` reads their quote at the view's current mark month. Declaring
a pool creates no position, cash movement or policy decision.

Assembled claims have a separate payer-scoped `Claim` projection, shared by
allocation's demand read. Claim amounts come from canonical assembly, not a
second evaluation of scheduled terms. The opening spending review occurs before
that assembly, so it does not expose this month's claims. No observation changes
the current review order or grouped settlement behavior.

`engine/claims.rs` assembles configured demands from the current month's terms,
live mortgages and assessed tax liabilities. It does not move money or decide
funding. Each occurrence has a rollout-local month/index handle independent of
its cause label; paid claims no longer appear in the due-claim view.

`engine/payments.rs` executes `PayClaim` and `Consume` requests using the same
canonical transfer, deduction, mortgage and tax effects. A claim payment names
the occurrence, exact full amount and an owned, declared funding account; the
claim fixes its recipient and effect. Consumption names a separate component,
another actor's recipient account and positive amount, without becoming a contract
claim. Transfers between one's own accounts are not consumption. Receipts retain
the caller's request ID, target, requested amount and paid/rejected outcome.
Invalid ownership, account, handle, amount, duplicate payment or insufficient
cash rejects before changing books or capture. Unexpected arithmetic/accounting
errors remain explicit simulator errors, not financial-failure receipts.

`engine/obligations.rs` retains the configured all-or-none funding-group control
and its existing demand/failure event projection, over that same executor.
Allocation reserves both configured claims and the separate consumption request.
The scoped action control instead follows the ordered execution described below.

## Scoped household action batches

`simulator.ActionSession(input_json, actor, rollout_ids, capture="forensic")` retains the input and
financial books in process. Python calls `start()`, then submits one batch to
`advance()` until it receives `Finished`. `Decision` rows carry original rollout
IDs and copied actor-scoped cash accounts, public positions, declared pools/quotes,
current claims and previous-month receipts. The caller keeps policy memory and
owns the outer loop; `engine::actors::Session` owns financial stepping, not callbacks.
`DecisionActions` returns
one ordered list for each active `(rollout_id, month)`; response order is immaterial.
Missing, duplicate, stale or unknown keys and cross-path/session claim handles are simulator
errors which close the session, not resubmittable actions. A caller may adapt
scalar authoring over these rows; there is no scalar actor-engine entry point.

Each list uses exact `Sell`, `Buy`, `Transfer`, `PayClaim` and `Consume` requests.
Canonical trade/payment/transfer operations own admission, atomic effects, lot
basis and taxes. Receipts retain executed or rejected requests, but not an
unattempted suffix. `Rollout::stop` distinguishes a rejected action (identified by
its month/index receipt) from unpaid due claims. Both preserve the stopped book
and exclude that path from later batches while other paths continue. Unexpected
accounting/arithmetic failures return a simulator error, not an action rejection.
Preparation and closing share the configured runner's financial implementations;
actor execution has no implicit pre/post allocation, harvesting, sale or payment.

`capture="summary"` omits detailed trace retention; `"dense"` adds financial event
tables and monthly books, and `"forensic"` adds the journal. Every mode returns the
same summary: account cash and public-pool gross marks over observed snapshots,
keyed payment outcomes, canonical tax records, unpaid claims, the exact ending
book and the last month's attempted action prefix. There is no post-stop padding.
Snapshot 0 is opening; snapshot `m + 1` is after event month `m`. The stopped final
book uses `ending_mark_month`, not future prices. Summary payment amounts follow
`payments::Receipt::amount_paid()`; an unfunded request is never partly paid.
Tax records remain selected canonical events, not preaggregated universal metrics.
Consumers choose their own account/component reductions and tax treatment.

Capture does not change policy observations: only the previous month's receipts
reach the next decision, even when all historical receipts are retained for replay.
Detailed output is `Rollout::trace`; its absence is not a zero-valued financial history.

This session supports one decision-making household and scripted
counterparties. It rejects configured allocation/harvesting/tender policies and
scheduled sales rather than silently bypassing them. Housing and private-equity
lifecycle inputs are not supported. Public trades use the explicit holding pools,
so an all-cash start can buy a previously unheld asset without a dummy lot or policy.
Current account and quote records are frozen typed objects; money and quantities
are exact integers with the declared currency/quantity scales. Prior receipts and
terminal results keep their canonical JSON encoding. No full future series or
mutable native books cross the boundary. Higher-level tax/contract observations
are not part of this initial binding.

```python
session = ActionSession(input_json, actor, original_ids)
try:
    batch = session.start()
    while not isinstance(batch, Finished):
        batch = session.advance(decide(batch))
    rollouts = json.loads(batch.rollouts_json)
finally:
    session.close()
```

Close releases retained input/books if a Python policy raises. The spending-only
prototype and configured full-run interfaces remain separate migration work;
new actor consumers use this session, not those controls.

The `engine/actors_test.rs` stories drive the same steps in a test-only harness and run with
`bbr test //finance/augur/rust:simulator_test`. They include contribution → bill →
chosen sale → explicit payment, canonical synthetic-tax assessment/payment, mixed
buy/transfer/buy ordering, prefix preservation, distinct unpaid-claim stops,
batch-native versus scalar-adapted selected replay and invalid response routing.
Their supplied paths and flat tax brackets are deterministic controls, not market
forecasts or claims of statutory tax coverage.
`bbr test //finance/augur/rust:action_test` exercises the real Python boundary,
including receipt-aware memory, complete-batch routing, selected replay and closure.

## Covered behavior

The acceptance suites in `sim/testing/` assert exact integer answers for:

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
  order, FIFO lot dispositions, declared sleeve identities, realized gains,
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
and must round-trip exactly through the `float64` external-series boundary the
sampler writes across; the Rust validator and Python adapter reject fixtures
that would lose an integer level during that conversion. Series coverage is deliberately dense:
every series supplies every rollout and snapshot in the fixture.

Private-equity channels use the same dense row-major fixture contract but keep
their distinct types explicit in the series names: mark/recovery/valuation are
currency quanta, capacity/eligibility/forced-sale fractions are exact PPB,
regime/event-kind are validated integer codes, and opportunity/blocked channels
are 0/1. The Python adapter reconstructs the typed `PrivateEquityBundle` only where the
sampled model hands one over; Rust never routes PE marks through ordinary
security-price series.

TLH policies encode every heuristic parameter as integer PPB. The maturity/drawdown
curve and give-back ledger use integer arithmetic. A sale allocates each lot the
difference between rounded cumulative proportions before and after that lot's
units: `round(H * sold_after / U) - round(H * sold_before / U)`. This conserves the
rounded total and assigns residual quanta in execution/lot order, with the receiving
lot's short-/long-term gain character.

Scheduled sales hold `H` and `U` at the month's opening sale phase and advance a
sold-unit cursor only after successful execution. Splitting the same ordered lot
sequence into requests therefore leaves its total and per-lot attribution unchanged.
Dynamic pool sales take a new `H/U` anchor for each trade; fragmentation can shift
a quantum between lots, but full liquidation still leaves exactly zero deferral.
These are reduced-form proportional rules, not statutory per-lot TLH. Acceptance
cases include odd-quantum partial/full liquidation, scheduled fragmentation,
mixed gain character and rejected-trade cursor preservation.

Initial lots store total basis and never a per-unit figure. A sale apportions
the basis a lot still holds by the units leaving it, so selling a lot down in
pieces consumes exactly what it held -- no divisibility precondition, and no
remainder stranded in an emptied lot. What money is and why it is not a decimal
crate: <docs/money_representation.md>.

Bond coupon rates use the same parts-per-billion contract and must round-trip
exactly through the `float64` boundary. Nominal coupons round the full
`face × annual rate × period / 12` rational once; TIPS carry an indexed
principal and a fixed-point period rate. Government
issuer levels come from one scenario-level jurisdiction identity registry,
rather than duplicated caller-supplied metadata on each bond.

Distribution tax-character fractions use exact PPB weights that must be
positive, sum to one, and preserve the same issuer identity contract as bond
interest. Each slice is paid, journaled, attributed, and routed through the
jurisdiction's interest-exemption policy independently; the slice sum is the
fund's cash payout.

Per-unit distribution paths are nonnegative: zero is an explicit no-payment month.
Missing or non-finite sampled values are rejected by the compiler, not filled with
zero. Security and home-value price paths still require strictly positive values.

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
Sleeve quantity scales are explicit input integers, taken during input preparation
from the sleeve's own asset, so the fixture cannot state a scale the Python side
does not use.

## Product read model

`simulate_product_metrics(fixture, primary_agent_id)` emits the seven base product metric
series plus the per-rollout failure month, under the compact capture mode — no monthly
snapshot, journal, or event trace. `backend.py` wraps it as the product API's
`ProductMetricArrays` and `ProductProjectionSummaries`, composing the derived metrics and
the percentile fan with `sim/metric_composition.py` and `sim/quantiles.py`.
Metric valuation and observation boundaries:
[docs/product_metrics.md](docs/product_metrics.md).

`sim/compiler/execution.py` connects this to a live request: the authored scenario,
supplied rules and materialized paths become the prepared integer execution input
once. The backend transports it without further financial compilation.

This engine serves all four projection endpoints, the selected rollout included.
`ProductService` holds an `Engine` (<../sim/backend.py>) rather than reaching for this
package directly, so everything above that contract — the derived metrics, the terminal
reduction, the percentile brackets, and the rollout projection — is written against the
canonical event frames rather than against this engine's output layout. The seam is what
makes a second engine possible; it is not evidence that one exists.

### What porting off the retired engine can and cannot change

Consumers outside this repo still call the JAX entry points this engine replaced, and each
one has to be ported. What that port is allowed to move is settled, and worth knowing before
anyone re-derives it from a diff of two runs.

Until the JAX engine was retired (`2d1d2a07c`), a differential suite compared both engines on
every commit — untagged targets, so `bazel test //...` ran them. `product_failure_test.py`
asserted **exact integer equality** of the per-rollout failure month and of every product
metric array, on the feature-rich scenario with an unfundable obligation, for two agents.
`product_scenario_test.py` did the same for one `ScenarioKey` against the fixture
deployment's own portfolio and sampled model, funded and after ruin.

So a port that moves a consumer from the JAX product entry points onto the `Engine` contract
should return the same failure vector and the same metric arrays, to the integer. A grid whose
numbers move across such a port has changed something else — most likely the sampler, since
`model/structural_macro.py` and `fit/structural_macro.py` were reworked over the same period.
Establish that separately before reading a difference as an engine difference.

The one place the engines were known to differ is event-frame recording **inside a failed
rollout's failure month**, and it is structural rather than a bug either side owns: Rust stops
inside its month loop at the phase that could not pay, so whether a phase was recorded depends
on where it sits in that order, while a vectorized scan reports the whole failure month or
none of it. No month-level rule reproduces an ordering within one month. Product metrics and
the failure vector were never part of that divergence.

Preparation, precision and the remaining language-binding boundary:
[docs/execution_boundary.md](docs/execution_boundary.md).

## Python extension

`simulator.so` is a `rust_shared_library` built from `python.rs`, imported as
`finance.augur.rust.simulator` and typed by the hand-written `simulator.pyi`. Fixtures
cross as JSON text because that is the simulator's input contract; results cross as Python
integers, so the fan workload never pays for a dense JSON round trip. `simulator_cli`
remains for out-of-process forensic runs.

Scenario features the fixture cannot express are refused rather than encoded without them:
[docs/execution_boundary.md](docs/execution_boundary.md).

## Layout

`engine.rs` is the orchestrator: the rollout month loop, the public entry points, and the
shared per-rollout state. Each policy family it drives lives in `engine/` beside it —
`validation`, `property`, `claims`, `obligations`, `taxes`, `securities`, `target_allocation`,
`private_equity`, `tlh`, `trades`, `cashflows`, `transfers`, `recorder`, `accounts`, `errors`. Submodules reach
the shared state through `use super::*`, and expose to the root only what it calls;
anything a module uses alone stays private to it, which the single 7.5k-line file could
not express.

The Rust half of the throughput benchmark lives in `benchmark/`. The
feature-rich scenario it measures is not Rust's and lives in
<../benchmark/scenario.py>; `benchmark/fixture.py` here only writes it out as the integer
document, which the standalone binary needs on disk and the in-process bindings do not.

## Targets

```text
//finance/augur/rust:simulator_cli
//finance/augur/rust:simulator_ext
//finance/augur/rust:simulator_test
//finance/augur/rust/benchmark:all
//finance/augur/rust/benchmark:fixture_bin
//finance/augur/rust/benchmark:driver_bin
```

`simulator_cli FIXTURE.json OUTPUT.json` retains full forensic traces. The Rust
benchmark driver's default `--output-mode dense` retains monthly state and
compatibility events; `--output-mode compact` selects the older terminal-summary
throughput workload. See [benchmark/README.md](benchmark/README.md) for the
measured baselines and their output-contract caveats.
