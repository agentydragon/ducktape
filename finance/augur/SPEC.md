# Augur — Specification

Augur is a financial simulator. An experiment supplies a financial situation,
loads or samples possible market paths, applies executable policies, and examines
outcome distributions and individual financial histories. The house-buying app
is one consumer, not the definition of the library.

This document describes current behavior and its limits. Broader capability
requirements live in <sim/REQUIREMENTS.md>; implementation responsibilities are
described in <sim/DESIGN.md> and the module documentation.

## Preparation and market inputs

Market generation and financial settlement are separate. Experiments choose
datasets, models, fitting, sampling and product construction; execution consumes
already supplied paths. Forecast-only evaluation does not require simulator
instrument declarations. Sharing a model does not imply it supports every
instrument or that its forecasts are adequate for a particular decision.

Preparation resolves the authored scenario, rules and supplied paths into one
self-contained typed value of exact monetary terms, resolved tax rules and paths.
Sessions and reports consume these facts directly; file/native serialization is
private, not a parallel mutable domain API. Execution does not reread the original
scenario or load evidence/tax configuration. Missing or non-finite required paths reject;
they are not synthesized as zero observations. Ordinary public-security and
home-value prices are positive. Prices used exclusively by reduced-form TLH
portfolios may be zero, allowing worthless exposure to be liquidated; negative
prices are invalid. Per-unit security distributions may explicitly be zero.

A supplied return series must be interpreted according to its product
construction. The existing total-return equity proxy is not a taxable
price-plus-dividend model. Current held dated-bond principal is not a tradable
market quote. Product/tax assumptions must remain explicit.

Identical prepared paths and ordered decisions produce reproducible financial
results. Original `rollout_id` values survive result selection/reordering;
an array position is not another identity. A policy's random choices and memory
belong to its caller and must also be reproduced for a selected replay.

Rolling-origin forecast reports retain individual origins and non-finite scores.
Their descriptive means do not establish IID uncertainty or significance when
history and targets overlap. Matching prediction-market quotes is distinct from
out-of-sample predictive quality.

## Common policy and session contract

There is one policy shape: a batch of active actor/path observations produces a
keyed batch of ordered action lists. The caller owns policy memory and the Python
monthly loop. Scalar authoring adapters use that same interface; there is no
separate native spending-amount or allocation-weight callback API.

The action session supports one decision-making household with scripted
counterparties, public securities, reduced-form TLH portfolios, cash, due claims
and held dated bonds. It does not support household housing or PE actions.
Configured scenario adapters use Python-controlled financial steps while retaining
their scripted housing/PE events and funding conventions; they do not provide an
alternative executable policy interface.

For each active path, the common session:

1. Applies scheduled financial events and component market updates, and assembles
   due claims. TLH advances before investor operations, including scheduled ones.
2. Exposes current actor-scoped observations, once that month.
3. Executes the submitted actions in caller order.
4. Stops on rejection or remaining unpaid claims, otherwise closes the month
   and prepares the next decision.

Observations include current owned accounts, public lots/basis, TLH statements, declared empty
holding pools and their current prices, due claims, recorded tax facts, held
dated-bond facts, and current/origin CPI when modeled. Missing CPI is explicit.
They do not expose another actor's private books, future realized paths or a
future tax assessment as a current liability. Copied observations cannot mutate
canonical books.

Exact lot sales, quantity purchases, cash transfers, claim payments and chosen
consumption are economic requests. Helpers may propose funding or rebalancing
actions, but execution does not invent trades, reorder sells before buys, cut
spending, or call policy again within the month. Current cash execution is
immediate; it does not model real settlement delays or implicit credit.

## Accounting and failure

Execution owns cash, positions, liabilities, gain/income facts and settlement.
Monetary journal entries balance and apply atomically. Amounts, quantities and
rounding have explicit fixed-point conventions.

A rejected action changes none of that action's financial state. Earlier
successful actions remain; its rollout stops without executing later actions or
future policy calls. An unpaid due claim is a separate stop cause. Invalid batch
routing/input and arithmetic/accounting defects are errors, not investment ruin.
Other paths are independent.

A claim fixes its recipient, due amount and financial effect; the actor selects
a permitted funding account. Occurrence identity is separate from its label.
Chosen consumption is distinct from claims, taxes and own-account transfers.
An unfunded consumption request does not itself create contractual debt.

Bare actor transfers require owned, declared, funded cash and cannot assign tax
character. Scheduled cashflows retain their declared income/deduction treatment;
their unconditional source debits may make a scripted counterparty negative.
That convention grants no overdraft permission to actor actions.

Trade execution validates account, lot, ownership and quantity without silently
selecting another lot or reducing an exact order. Sales consume the selected
lots' actual basis, including residual basis on full disposal. Acquisition basis
comes from the actual settled purchase. FIFO is a caller's selection rule, not
the only possible exact-lot request.

An opening lot supplies its exact remaining total cost basis in the scenario's
currency quantum. That total need not divide into currency-quantized per-unit
amounts. Imports, execution and recorded lot state retain the total without
deriving and re-quantizing a per-unit basis; sales apportion it and full
liquidation consumes the remainder.

The configured runner retains all-or-none funding groups and automatic allocation
controls. Those are not common-session settlement requirements.

A reduced-form TLH portfolio owns its internal holdings and adjusted basis in
Python. The household observes its value and reported tax basis, and chooses
contributions, gross-cash withdrawals or liquidation. Modeled losses reduce the
same basis later consumed by redemptions; new contributions do not inherit prior
loss adjustments. Component value is counted once in household wealth.
Canonical accounting settles its financial effects and determines household tax.
The approximation does not reconstruct constituent trades or establish statutory
TLH fidelity. See <docs/tlh.md> for model and numerical conventions.

## Preserved configured capabilities and limits

Configured scenarios support property purchase/ownership, mortgages, carrying
costs, rent/occupancy changes, disposal and PE liquidity events. These are not yet
callable housing/PE actions in the common session. Changing a policy does not
make an existing contract cease to exist.

Mortgage installments, interest and payoff reconcile to the outstanding principal
in the liability ledger. Servicing state and reporting do not maintain independent
authoritative loan balances. An unpaid installment does not count as paid interest.

Property valuation grows the nominal purchase price by the supplied home-value
path since purchase. Pre-purchase index changes do not alter the purchase anchor.
Sale costs, loan payoff and tax basis are separate; no property sale is implied
merely by reaching the horizon.

PE paths distinguish marks, voluntary opportunities, eligibility/capacity,
liquidity blocks and forced sale/recovery. A stated recovery cashout is a total
for the remaining position, not a per-unit quote; proceeds and disposed basis
reconcile to it. The configured path still depends on tender-policy setup even
for compulsory recovery. It does not yet guarantee policy-independent compulsory
events or require explicit responses to every voluntary opportunity.

Taxes are settled financial consequences, not a terminal-wealth haircut.
Supplied rules and declared supported classifications determine assessments and
payments. This is not a tax-return preparation service or proof of complete
statutory coverage. Managed-account heuristics, unsupported distribution character,
law-year/residency gaps and housing-basis limitations must not be presented as
validated fidelity. Adding a jurisdiction can require mechanics, not merely data.

## Outputs and observation boundaries

Typed common results retain original path IDs, request/claim/component/account
identities, attempted receipts, canonical tax facts and exact stopped/ending books.
Optional detailed capture includes books, journal and columnar events; compact
capture does not require those histories. Missing capture is not zero activity.

A stopped event month has a closing book valued at its already-observed marks,
not at an unobserved future price. Post-stop months are unobserved. Selected
replay agrees with the common results on the same inputs and fresh policy state.

Policy intentions, attempted requests, actual paid consumption, sale proceeds
and taxes are distinct quantities. Product shortfall sums unpaid due claims and
valid attempted consumption gaps; malformed actions and unattempted consumption
do not invent money shortfalls. Their stop causes remain visible.

Held-bond principal disappears when redemption is processed and cash replaces it;
future coupons or indexed proceeds are not current spendable cash. Par/indexed
principal capture does not establish off-par trading or full TIPS tax treatment.

The product API provides selected metric fans and per-path detail. Wealth
terminal distributions use completed horizons; monthly fans use that month's
observed population, not eventual survivors. Unobserved values are null. Selected
stopped paths retain their actual closing books. Projections reject unsupported
or uncaptured holdings instead of inventing zero values.

Selected detail is sampled/simulated on request without a server rollout cache.
The browser discards obsolete selected detail and ignores stale responses;
displayed scenario fans remain separate request state. No general optimizer,
strategic multi-agent economy, cross-border tax model, partial-payment recovery
or broker settlement-delay system is implied.
