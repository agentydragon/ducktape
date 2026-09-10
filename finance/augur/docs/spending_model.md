# Spending and allocation decisions

An experiment chooses its decision rules; Augur owns accounting, lots, funding,
taxes and settlement. The experiment combines supplied market paths, initial
holdings/contracts and a strategy. It does not need to encode every rule shape
in an engine-side union.

## Native callable boundaries

`rust/engine/spending.rs` accepts a factory producing a fresh function per
rollout. At each live month's opening, the function observes current cash,
public holdings valued at current prices, and origin-relative CPI. Cash and
holdings are agent-wide; these values are not after-tax liquidation proceeds.
It returns nominal consumption in the input's currency quanta. The function
owns review cadence and memory. Zero requests create no payment action; negative
requests and function errors reject the run.

The Python allocation example uses `ActionSession` and ordinary batch functions
after monthly cashflows and claim assembly. <../policy/sleeves.py> proposes exact
trades from named account/asset pools, current lots and prices. The policy owns
weights, cash reserves and drift cadence; it submits trades and payments in one
ordered action list. Zero targets remain sellable and receive no deposits; a
full exit selects every unit, including rounded-zero marks. At least one weight
must be positive. An all-zero vector does not imply a cash allocation.

Neither observation supplies future market paths or mutable books. Python session
selection preserves input IDs and caller order; each selected replay constructs
fresh decision state. Policies must not share mutable state between paths or depend
on invocation order.

Runnable consumers are <../x/bounded_spending/README.md> and
<../x/allocation_glide/README.md>. A constant or glide target is an experiment
rule, not an implicit claim to reproduce any particular published study.

## Demands and settlement

Chosen consumption and configured claims are separate payment inputs.
Claims retain their canonical amount, recipient and financial effect; their
occurrence handles are distinct even when cause labels collide. Consumption
names its own component and amount without creating a claim. Full claim payments
and consumption use one executor and produce request-identified receipts.

Opening review precedes current cashflows/events. The engine then assembles
configured claims, including tax payments due; allocation raises cash for claims
and the separate chosen consumption; same-source
funding groups settle all-or-none; only then do surplus purchases execute.
Purchases reserve the assembled demands and are clamped to cash available
after settlement. This grouping is the current runner's explicit control, not
part of an individual payment action. A spending cut must be an authored decision, not a
second liquidation or tax calculation hidden in the experiment.

The cash band controls when to raise/invest. Quiet-band drift rebalancing
suppresses itself whenever that band is active. A changed target therefore
does not guarantee immediate full reallocation. `allow_purchases=False`
leaves surplus cash uninvested and disallows drift rebalancing.

An unfunded group stops the path; it does not automatically cut spending,
prioritize taxes/rent, partially pay consumption, or roll back earlier funding
sales. Failed paths receive no later decision callbacks. Failure means the
authored demands could not settle under the configured mechanics, not that
every cheaper lifestyle was tried.

## Reporting and scope

Requested consumption, paid consumption, gross sale proceeds, tax payments and
contract payments are distinct. Compact consumption captures the specific policy
component's actual settlement receipt, excluding other claims even with the same
cause ID or category. A broader lifestyle measure must explicitly include
eligible contract categories and exclude principal repayments and duplicate
expenses. Selected traces retain canonical lot/tax/settlement evidence.

There is no built-in ladder, forced fallback tier, costless relocation,
contract cancellation, partial-consumption priority or recovery rule. These
require explicit decision and settlement semantics. An experiment may author
a ladder with local function state, but must declare its transition costs,
reversibility and limitations; changing location also needs supported
contracts, currency and tax treatment.
