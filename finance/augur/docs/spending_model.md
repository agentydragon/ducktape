# Spending and allocation decisions

An experiment chooses its decision and funding rules; Augur owns accounting, lots,
taxes and settlement. The experiment combines supplied market paths, initial
holdings/contracts and a strategy. It does not need to encode every rule shape
in an engine-side union.

## Python batch policies

Spending and allocation examples use `ActionSession` and ordinary batch functions
after monthly cashflows and claim assembly. The caller owns the monthly loop and
policy memory keyed by original path ID. Current cash and public holdings are gross
values, not after-tax liquidation proceeds. Current/origin CPI is an exact ratio
when supplied, explicitly absent otherwise. <../policy/sleeves.py> proposes exact
trades from named account/asset pools, current lots and prices. The policy owns
weights, cash reserves and drift cadence; it submits trades and payments in one
ordered action list. Zero targets remain sellable and receive no deposits; a
full exit selects every unit, including rounded-zero marks. At least one weight
must be positive. An all-zero vector does not imply a cash allocation.

Observations supply neither future market paths nor mutable books. Python session
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

After scheduled cashflows and claim assembly, one policy call returns an ordered
action list. An unexecutable action changes no books and stops that path; earlier
successful actions remain and later actions/months are unattempted. An unpaid due
claim after successful execution is a distinct fatal stop. No implicit funding,
spending cut, payment priority or retry is supplied by the executor. Zero chosen
consumption is represented by omitting the action, not a zero-valued payment.

Existing configured full-run consumers are a separate migration boundary: they
raise cash for their assembled claims, settle same-source groups all-or-none, then
invest surplus and run configured strategies. They have no callable spending API.
Their cash-band/drift controls and grouped-funding semantics remain until those
consumers move to explicit actions; do not mistake them for the action contract.

## Reporting and scope

Requested consumption, paid consumption, gross sale proceeds, tax payments and
contract payments are distinct. Compact actor results capture the specific policy
component's actual payment receipt, excluding other claims even with the same
cause ID or category. A broader lifestyle measure must explicitly include
eligible contract categories and exclude principal repayments and duplicate
expenses. An unattempted consumption action on a stopped month has no known
requested amount but paid zero; post-stop months are not observations. Selected traces retain canonical
lot/tax/settlement evidence.

There is no built-in ladder, forced fallback tier, costless relocation,
contract cancellation, partial-consumption priority or recovery rule. These
require explicit decision and settlement semantics. An experiment may author
a ladder with local function state, but must declare its transition costs,
reversibility and limitations; changing location also needs supported
contracts, currency and tax treatment.
