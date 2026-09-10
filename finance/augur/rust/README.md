# Augur simulator

The native financial kernels used by Python-owned action and configured drivers.
Prepared facts originate in `finance/augur/sim`; `execution.rs` declares the private
native input and financial result records. Validation runs once before execution.

`engine/world.rs::Prepared` validates a prepared run and creates one `World` per
selected path. Each world owns financial books and exposes private opening,
transaction, settlement, closing and capture operations. `sim/session.py` owns
month sequencing, active paths, receipts and stop lifecycle; `sim/configured.py`
uses that same Python session for the remaining configured control. There is no
production native session or full-horizon runner.

## Invariants

- Money is always a checked `i64` count of the input's declared currency
  quantum. Products use `i128` intermediates and explicit half-away-from-zero
  rounding.
- Every monetary change is a balanced compound journal entry. Entries are
  validated and applied atomically; signed debits sum to zero.
- Exogenous paths are sampled in Python and materialized once into a strict
  integer input. Rust does not resample paths.
- Rollouts retain original IDs and selection order; Python owns policy and month sequencing.
- The configured runner groups due claims by payer/source
  account and settles each group all-or-none. This is an explicit control, not a
  restriction imposed by individual payment execution.
- Failed rollouts stop executing future actions and preserve the actual stopped
  book and causal trace. No later forensic snapshots or events are emitted.
- Full forensic output and compact output use the same financial operations.
  Compact capture does not allocate every monthly book or journal.

The Python configured driver's dense/forensic outputs retain monthly books and
canonical event records; forensic adds balanced journals. Its compact mode retains
terminal summaries without allocating dense books. Capture never selects a different
financial evaluator.

Python <../x/bounded_spending/README.md> and <../x/allocation_glide/README.md>
consumers submit explicit trades and payments through `ActionSession`; there is
no separate native amount/weight callback API. Optional <../policy/sleeves.py> helpers
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
capital-gain rows. Journal and receipt counters are checked before
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
resolves an actor's declared cash accounts and public price rows; current
observations and product capture use that scope. Allocation instead selects its
funding account and declared source pools/sleeves. A borrowed `LotView` shares
per-lot valuation: round each lot to currency quanta before adding values.
Neither scope implies after-tax liquidation proceeds. Callers supply the observed
mark month; a stopped book uses its failure-event marks, never future prices.
CPI in the Python observation is current inflation relative to path origin.

Dated bonds carry par or current indexed principal, not a tradable market quote.
Their position lifetime follows processed events separately from valuation marks:
a bond redeemed in event month `m` remains principal in snapshot `m`, then cash
replaces it in snapshot `m + 1`. A horizon ending at `m` has not executed that
redemption. Failed closings retain the event month's CPI and the actually processed
redemptions, independent of the later snapshot label.

`engine::observations::ActorBooks` reads account, remaining public-lot/basis
and recorded tax facts for native observation projection.
The view fixes the actor and mark month; it cannot read
another actor's books or request future path values. Tax views expose recorded
income, jurisdiction facts and assessed outstanding liabilities, not hypothetical
future assessments. These tax views are currently native-only; the Python Observation
does not yet expose them. Scheduled purchases are not originated contracts.
Python-owned TLH components expose only their scoped current value and reported tax
basis. Their cohorts are not ordinary public lots. Native component basis control
accounts reconcile financial statements; they never calculate or own cohort basis.

`holding_pools()` also exposes owned public pools with no remaining lots, and
`public_price(asset_id)` reads their quote at the view's current mark month. Declaring
a pool creates no position, cash movement or policy decision.

Assembled claims have a separate payer-scoped `Claim` projection, shared by
allocation's demand read. Claim amounts come from canonical assembly, not a
second evaluation of scheduled terms. The monthly actor review follows that assembly and component advance. Reading an
observation does not reapply either phase.

`engine/claims.rs` assembles configured demands from the current month's terms,
Python-supplied mortgage installments and assessed tax liabilities. It does not move money or decide
funding. Each occurrence has a rollout-local month/index handle independent of
its cause label; paid claims no longer appear in the due-claim view.

`engine/payments.rs` executes `PayClaim` and `Consume` requests using the same
canonical transfer, deduction, mortgage posting and tax effects. A claim payment names
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
Configured allocation reserves the assembled claims.
The scoped action control instead follows the ordered execution described below.

## Scoped household action batches

`sim.session.ActionSession(prepared, actor, rollout_ids, capture="forensic")` accepts a
`sim.prepared.CompiledRun` of typed resolved facts. The binding privately serializes
it once; callers do not read or mutate a wire dictionary. `rust.invocation` writes
this value to a file and reads it back as the same typed object. The session retains the input and
financial books in process. Python calls `start()`, then submits one batch to
`advance()` until it receives `Finished`. `Decision` rows carry original rollout
IDs and copied actor-scoped cash accounts, public positions, held dated bonds, declared pools/quotes,
current claims, current/origin CPI and previous-month receipts. CPI is explicitly
absent when no index was supplied; it is not assumed flat. The caller keeps policy memory and
owns the outer loop. Python-owned action and observation models live in
`sim.actions` and `sim.observations`; the private native boundary carries serialized
financial facts, not public PyO3 domain classes.
`DecisionActions` returns
one ordered list for each active `(rollout_id, month)`; response order is immaterial.
Missing, duplicate, stale or unknown keys and cross-path/session claim handles are simulator
errors which close the session, not resubmittable actions. A caller may adapt
scalar authoring over these rows; there is no scalar actor-engine entry point.

Each list uses exact `Sell`, `Buy`, `Transfer`, `PayClaim`, `Consume`, and managed
`Contribute`, `Withdraw` or `Liquidate` requests from `sim/actions.py`.
`sim/observations.py` defines frozen current facts and decision rows. These are
Python-owned domain objects, not public PyO3 wrappers. Historical claim-payment
receipts retain the occurrence ID without retaining an executable session handle.
Canonical trade/payment/transfer operations own admission, atomic effects, lot
basis and taxes. Receipts retain executed or rejected requests, but not an
unattempted suffix. `Rollout::stop` distinguishes a rejected action (identified by
its month/index receipt) from unpaid due claims. Both preserve the stopped book
and exclude that path from later batches while other paths continue. Unexpected
accounting/arithmetic failures return a simulator error, not an action rejection.
Preparation and closing share the configured runner's financial implementations;
actor execution has no implicit pre/post allocation, investor sale or payment.
The Python-owned manager advances before the household review; that is an exogenous
component operation, not an investor Harvest action.

`capture="summary"` omits detailed trace retention; `"dense"` adds financial event
tables and monthly books, and `"forensic"` adds the journal. Every mode returns the
same summary: account cash, public-pool gross marks, dated-bond principal and
opaque TLH value over observed snapshots,
keyed payment outcomes, canonical tax records, unpaid claims, the exact ending
book and the last month's attempted action prefix. There is no post-stop padding.
Snapshot 0 is opening; snapshot `m + 1` is after event month `m`. The stopped final
book uses `ending_mark_month`, not future prices. Summary payment amounts follow
`payments::Receipt::amount_paid()`; an unfunded request is never partly paid.
Tax records remain selected canonical events, not preaggregated universal metrics.
Consumers choose their own account/component reductions and tax treatment.

`Observation.held_bonds` contains only owned, unredeemed dated bonds. Each frozen
`HeldBond` has its ID/account, issuer jurisdiction, face/purchase amounts, a fixed
nominal coupon amount or indexed annual rate, coupon period, purchase/maturity
months and current principal. Known contractual dates are observable; future CPI or future
indexed payments are not. The observation follows scheduled processing, so a
redemption due this event has already become cash and that bond is absent.
Principal is a par/indexed carrying amount, not a tradable quote or liquid balance;
`public_holdings` still excludes these bonds. No bond trade actions are available.

`summary.bond_principal` carries `{account, bond_id, values}` rows scoped to the
actor. It uses the same canonical principal calculation as full books, retains
zero after redemption, and ends at the last observed snapshot without padding.
Summary mode stores these numeric series, not monthly bond books or cashflow traces.
Par-only held-to-maturity and existing issuer-exemption mechanics are preserved;
this interface does not certify full TIPS or off-par tax coverage.

Capture does not change policy observations: only the previous month's receipts
reach the next decision, even when all historical receipts are retained for replay.
Detailed output is `Rollout::trace`; its absence is not a zero-valued financial history.

This session supports one decision-making household and scripted
counterparties. It rejects configured allocation/tender policies and
scheduled sales rather than silently bypassing them. Housing and private-equity
lifecycle inputs are not supported. Public trades use the explicit holding pools,
so an all-cash start can buy a previously unheld asset without a dummy lot or policy.
Current account and quote records are frozen typed objects; money and quantities
are exact integers with the declared currency/quantity scales. Prior receipts and
terminal results are typed records from `sim.results`; their action, payment and
stop variants have explicit `kind` tags. No full future series or
mutable native books cross the boundary. Higher-level tax/contract observations
are not part of this initial binding.

```python
from finance.augur.sim.session import ActionSession
from finance.augur.sim.results import Finished

session = ActionSession(prepared, actor, original_ids)
try:
    batch = session.start()
    while not isinstance(batch, Finished):
        batch = session.advance(decide(batch))
    rollouts = batch.rollouts
finally:
    session.close()
```

Close releases retained input/books if a Python policy raises. Bounded spending
and monthly actions use this session for their Python-owned policy loops.

`Finished.rollouts` preserves the requested original ID order. Each rollout has a
typed `summary`, optional `stop`, and optional `trace`. A trace contains typed
historical books, journal, bond/distribution cashflows and receipts, plus the
existing columnar `EventLog` for event queries. Python boundary codecs decode the
private transport once; domain consumers do not parse JSON or retain a second
raw result tree. For file I/O, use `Finished.model_dump_json()` and
`Finished.model_validate_json()`; an individual replay uses the same methods on
`Rollout`. The configured-engine forensic adapter is separate and remains until
its remaining callers migrate.

The `engine/actors_test.rs` controls call one world's financial operations directly:
opening cashflow → sale → claim payment, synthetic-tax assessment/payment, mixed
buy/transfer/buy ordering, atomic rejection, retained lot basis and compact/forensic
agreement at explicit stopped marks. They run with
`bbr test //finance/augur/rust:simulator_test`; their supplied paths and flat tax
brackets are deterministic controls, not market forecasts or statutory coverage.
`bbr test //finance/augur/rust:action_test` exercises the real Python boundary,
including ordered prefixes, unpaid-claim stops, receipt-aware memory,
complete-batch routing, selected replay and closure.

## Covered behavior

`bbr test //finance/augur/rust:bond_test` runs the nominal/indexed coupon,
income-character and redemption acceptance cases through the Python action session
and typed books/receipts. Its policy pays observed claims in order; the unfunded
accretion-tax case retains received coupon cash and stops without rescue. Shared
bond-case construction stays in `sim/testing/bonds.py`. The actual product bond
carrying-value regression now lives in `product/test_action_projection.py`, reducing
the same session's captured principal histories rather than rerunning a configured engine.

`bbr test //finance/augur/rust:security_distributions_test` runs the public-fund
payout, issuer-exemption and sub-quantum payment controls through the same typed
session. The policy only pays observed claims; distribution cash and source income
remain canonical engine facts. Compiler controls share the authored cases in
`sim/testing/security_distributions.py`, not an alternative result adapter.

`bbr test //finance/augur/rust:transfers_test` runs scheduled and recurring scripted
cashflows through the common session with Alice as the sole decision actor. Exact
cash books include the scripted counterparties, preserving conservation checks
across 1,000 identical paths without retaining the configured test runner.

`bbr test //finance/augur/rust:indexed_payments_test` checks annual rent-index
resets on distinct paths, incoming indexed transfers, and explicit payment of
observed indexed claims through the common session. It also pins half-up
sub-cent scaling and cashflow-before-payment timing with conserved cash.

`bbr test //finance/augur/rust:obligations_test` runs household claim funding,
account-scoped sales, optional cash bands and failure controls through explicit
Python batch actions. A later rejected payment preserves earlier sales/payments:
two $500 bills against $600 pay the first bill, reject the second with $100
unchanged, and skip later actions. Stopped paths receive neither future scheduled
cashflows nor policy calls; another path in the same batch can continue.

The remaining legacy acceptance suites in `sim/testing/` assert integer answers for:

- opening balances and opening equity;
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
- mortgage origination/payment/payoff postings and same-source funding-group
  settlement using Python-computed installments, plus property-tax carrying costs;
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
- Python-owned TLH model effects settled through the same native cash/tax journals,
  with atomic rejection, scoped reported values/basis and honest aggregate financial events;
- insufficient-cash failure month and state freezing;
- federal and California ordinary-income year-end tax accruals;
- federal SALT itemization from funded property tax plus sibling state-income
  tax, including forward-filled year-indexed cap schedules;
- federal long-term-capital-gain stacking and tax accrual;
- quarterly estimated-tax payments, aggregate safe-harbor Q4 computation,
  January true-up, tax-liability settlement, and funded/unfunded tax-payment
  events.

The Rust ledger also records tax expense/liability accrual entries, tax
prepayments and settlement, and nets capital gains/losses once per taxpayer so
one shared ordinary-loss offset and carryforward feed every jurisdiction in
later tax years. Monthly and terminal output retain jurisdiction-level tax-liability
state and held bond principal; selected traces expose tax-payment,
tax-settlement, and issuer-attributed bond cashflow/accretion records.
Monthly and terminal books retain taxpayer capital gains plus reported component
value/basis statements. Compact capture retains those ending facts, not private cohorts
or a cumulative give-back mirror.
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

The TLH model and exact cohort arithmetic live in <../sim/tlh.py>. It advances at
the current mark before any investor operations, including configured scripted sales.
Unlike the retired native pooled give-back rule, new contributions retain their own
basis and do not inherit older loss adjustments. Contributions first participate in
the following month's manager advance. Already-settled manager effects survive a
later rejected household action.

`engine/components.rs` validates supplied effects before posting: household cash
plus the change in reported basis must equal signed capital gains plus declared
interest income. A malformed effect is a programming/accounting error, not simulated
ruin. Investor affordability/ownership rejection occurs before committing a Python
candidate or household books. Native settlement does not infer rounded component NAV
from the prior rounded mark. Dense/forensic events state component realizations,
contributions and redemptions without pretending to be constituent lot dispositions.

Initial lots store total basis and never a per-unit figure. A sale apportions
the basis a lot still holds by the units leaving it, so selling a lot down in
pieces consumes exactly what it held -- no divisibility precondition, and no
remainder stranded in an emptied lot. What money is and why it is not a decimal
crate: <docs/money_representation.md>.

Nominal bonds carry one fixed coupon amount compiled by `sim/bonds.py`, rounding
the full `face × annual PPB rate × period / 12` rational once to currency quanta.
The executor pays that amount on each contractual coupon date. Indexed bonds
instead carry an annual PPB rate and retain their dynamic principal/period-rate
calculation. The input and owned `HeldBond.coupon` observation distinguish fixed
amounts from indexed rates; no nominal annual rate or indexing flag duplicates
the payment term. Indexed rates still require the existing exact `float64`
round trip. Government
issuer levels come from one scenario-level jurisdiction identity registry,
rather than duplicated caller-supplied metadata on each bond.

Distribution tax-character fractions use exact PPB weights that must be
positive, sum to one, and preserve the same issuer identity contract as bond
interest. Each slice is paid, journaled, attributed, and routed through the
jurisdiction's interest-exemption policy independently; the slice sum is the
fund's cash payout.

Per-unit distribution paths are nonnegative: zero is an explicit no-payment month.
Missing or non-finite sampled values are rejected by the compiler, not filled with
zero. Ordinary security and home-value prices must be positive. A security series
used exclusively by TLH portfolios may reach zero for worthless-exposure
liquidation; negative prices remain invalid.

`policy/configured_allocation.py` proposes configured allocation using the shared
Python sleeve helpers. It evaluates the band after monthly obligations have
accrued and proposes sales before the grouped funding check. An unpaid group means
that this configured strategy did not fund it, not that no possible strategy could.
Buy
orders are decided from that same pre-settlement observation but execute only
after obligations settle, with a floor affordability clamp against then-current
cash. Each ordinary purchase gets a path-local policy/sleeve lot identity and
records the rollout's observed price and acquisition month. Purchased lots join
the first configured source-account pool for later FIFO sales. Selected traces
expose the source and proceeds accounts on every lot disposition and the ordered
sleeve identities attempted for every matching obligation. Optional quiet-band
drift rebalancing is all-or-nothing, returns every sleeve to its floored target,
and suppresses itself whenever the cash band is already raising or investing.
Sleeve quantity scales are explicit prepared integers taken from the sleeve's own asset.

## Mortgage boundary

Mortgage terms, fixed installments, active state and paid-interest YTD belong to
`sim/mortgage.py`. The ledger owns outstanding principal. Native posting operations
validate supplied immutable payment facts; year-end assessment consumes supplied
interest facts, and capture retains read-only statements. Neither capture nor
tax assessment owns a mutable loan mirror. Configured lifecycle conventions:
<../docs/rental_and_lifecycle.md>.

## Product read model

`simulate_product_metrics(prepared, primary_agent_id)` emits the seven base product metric
series plus the per-rollout failure month, under the compact capture mode — no monthly
snapshot, journal, or event trace. `backend.py` wraps it as the product API's
`ProductMetricArrays` and `ProductProjectionSummaries`, composing the derived metrics and
the percentile fan with `sim/metric_composition.py` and `sim/quantiles.py`.
Metric valuation and observation boundaries:
[docs/product_metrics.md](docs/product_metrics.md).

`sim/compiler/execution.py` connects this to a live request: the authored scenario,
supplied rules and materialized paths become the prepared integer execution input
once. The backend transports it without further financial compilation.

The configured Python driver serves all four projection endpoints, the selected rollout included.
`ProductService` holds an `Engine` (<../sim/backend.py>) rather than reaching for this
package directly, so everything above that contract — the derived metrics, the terminal
reduction, the percentile brackets, and the rollout projection — is written against the
canonical event frames rather than against native storage. The common action-session
projection in `product/action_projection.py` uses the same financial facts; it does
not yet replace the configured app's housing/PE and grouped-funding capabilities.

Preparation, precision and the remaining language-binding boundary:
[docs/execution_boundary.md](docs/execution_boundary.md).

## Python extension

`_simulator.so` is the private extension built from `python.rs`, typed by
`_simulator.pyi`. Its private `_PreparedWorlds` and `_World` expose financial
primitives, not action/observation classes or a session lifecycle. Public callers
use `sim/session.py` or the remaining `sim/configured.py` control. Python codecs
lower prepared facts and requests privately and decode observed facts and financial
results once; Python adds its receipts and stop state. File serialization belongs
to explicit I/O callers.

Scenario features the prepared input cannot express are refused rather than encoded without them:
[docs/execution_boundary.md](docs/execution_boundary.md).

## Layout

`engine.rs` retains shared financial state and opening/closing kernels. Native
modules include `property`, `claims`, `obligations`, `taxes`, `securities`,
`world`, `actors`, `private_equity`, `components`, `trades`, `cashflows`,
`transfers`, `recorder`, `accounts`, `errors` and `validation`.
`world.rs` exposes the private financial calls used by both Python drivers.
Actor tests exercise these calls directly. Remaining test-only configured
`engine.rs::simulate*` helpers still drive existing acceptance readers; they are
not exported production runtimes.

## Targets

```text
//finance/augur/rust:simulator_ext
//finance/augur/rust:simulator_test
```
