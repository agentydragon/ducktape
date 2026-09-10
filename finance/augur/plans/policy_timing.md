# Policy timing: executable cases and remaining choices

Scoped timing contract and remaining **GP** choices in [the landing plan](roadmap.md).
The Python spending example exercises the common [actor-facing interface](policy_interfaces.md).
Cases A/B retain configured-runner accounting controls while those consumers migrate;
they no longer use an executable spending callback.

## Name the cashflows, not just “spending”

Reporting terms (dimensions, not a separate native spending-summary API):

| Term                    | Meaning                                                                                  | Not equivalent to                                                                                    |
| ----------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `asset_sale_proceeds`   | Gross cash raised by asset sales; separate fees when modeled.                            | Realized gain, consumption, or all cash available.                                                   |
| `consumption_requested` | Demand for an identified consumption component; here, the policy's discretionary budget. | A sale order, tax-inclusive funding requirement, or an unchosen lifestyle anchor.                    |
| `consumption_paid`      | The amount settled for that same demand.                                                 | All non-tax outflows: purchases, principal repayments and own-account transfers are not consumption. |
| `tax_paid`              | Cash paid to taxing authorities, with refunds/prepayments separately identifiable.       | Tax accrued, taxable income, or money spent on the chosen lifestyle.                                 |
| `contract_payments`     | Payments due under existing commitments, with their own categories.                      | Entirely consumption: rent and interest differ from loan principal.                                  |

For an attempted action, requested/paid consumption comes from its canonical
component-identified payment receipt, independently of claims' cause labels.
The bounded example projects live omitted zero requests as zero. A stopped month
without an attempted component receipt has unknown requested consumption and zero
actual payment; post-stop months are absent. A policy's unattempted intention is
not a paid or rejected request. Do not derive
consumption by subtracting taxes from sale proceeds or summing all transfers to
an outside actor. A broader lifestyle-consumption measure must explicitly combine
policy consumption with eligible contract categories without counting principal
or double-counting rent already included in a budget.

These examples use nominal USD cents, one supplied deterministic path, constant
prices/CPI, zero cash bands, cashflow-only allocation and no fees, distributions,
borrowing or housing assets. The figures are synthetic accounting controls, not
forecast results. The tax example's flat rates are not a jurisdiction's statutes.

## Configured case A: a smaller demand versus an unfunded group

`rust/engine/tests.rs::configured_timing_low_and_unfunded_consumption` starts with
$100 cash and $1,000 of sellable stock; an existing $700 rent demand is due in
month 0. The policy's consumption component excludes rent; rent is also household
consumption, despite being committed. Taxes are explicitly absent. The two inputs
declare $500 and $300 consumption, respectively. No native callback chooses the
amount, and settlement does not invent a cut.

| Month-0 quantity                          |        Rigid policy |          Cut enabled |
| ----------------------------------------- | ------------------: | -------------------: |
| `consumption_requested`                   |                $500 |                 $300 |
| `asset_sale_proceeds`                     |              $1,000 |                 $900 |
| Cash after funding sales, before payments |              $1,100 |               $1,000 |
| `consumption_paid`                        |                  $0 |                 $300 |
| Rent paid                                 |                  $0 |                 $700 |
| Outcome                                   | Funding group fails | Both payments settle |

The rigid policy is only $100 short of funding the whole group, but has $500 of
unpaid consumption and $700 of unpaid rent. Those quantities answer different
questions. Current same-source all-or-none settlement does not partially pay
consumption or prioritize rent, and does not undo the preceding funding sale.
The smaller amount is a different input before funding, not an automatic response
invented by settlement. The common action session instead executes payments in the
caller's order and preserves a successful prefix; this table is not its contract.

## Configured case B: consumption, tax payment and surplus investment

`rust/engine/tests.rs::configured_timing_surplus_investment_reserves_tax_and_consumption` starts
from the same $100 cash and $1,000 stock, with $500 basis. No rent. The synthetic
tax profile charges long-term gains at 10%, ordinary income at 20%, no deduction,
and no prior-year tax target. Compare investing surplus versus leaving it in cash
using the configured `allow_purchases` choice. Consumption amounts are predefined
claims, not a within-path callback; the test inspects opening books without making
decisions or mutating them.

| Event month | Opening books / predefined demand             | Execution                                                                                                                 |
| ----------- | --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| 0           | $100 cash/$1,000 stock; $500 consumption due. | Sell $400, using $200 basis and realizing $200 gain. Pay $500 consumption. No tax payment yet.                            |
| 1–10        | $0 cash/$600 stock; no consumption due.       | No additional sales or consumption.                                                                                       |
| 11          | Same books and no consumption due.            | Accrue $20 tax on the $200 gain at year-end. Accrual is not a cash payment.                                               |
| 12          | $0 cash/$600 stock; $50 consumption due.      | A supplied $100 nontaxable contribution arrives. Pay $50 consumption and $20 tax; only the remaining $30 can be invested. |

Both arms pay $550 consumption and $20 tax. With reinvestment enabled the last
snapshot has a new $30-basis lot and no cash; otherwise it has $30 cash and no new
lot. Payment timing here is **current synthetic engine behavior**, not a claim
that January true-up represents statutory filing/payment deadlines. The
[tax checklist](tax_coverage.md) tracks that gap separately.

The configured phase order is: apply current cashflows/events → assemble demands →
funding sales → grouped settlement → surplus
purchases → year-end assessment when due. A failed group stops subsequent
purchases, but is not a rollback of every action earlier in the month.

Run the two cases without network evidence downloads:

```bash
bbr test //finance/augur/rust:simulator_test --test_arg=configured_timing_
```

## Monthly action contract

One call per active actor per month returns an ordered action list. Submit the
whole list at once and execute it in the policy's order, using each action's
resulting books for the next action. No global sells-before-buys pass is implied.
There is no within-month policy callback or retry.

An unexecutable action is fatal for its rollout: it changes no books, earlier
successful actions remain recorded, and later actions/months are not executed.
Other rollouts continue. This is per-action atomicity, not an all-or-none month.
Receipts inform next-month policy decisions or the terminal report. A sequence
such as `[Sell(...), Buy(...), Transfer(...), Buy(...)]` is submitted by one call;
its cash dependencies must be executable under the chosen settlement rules.

## First actor-action example: agreed timing

Use one decision-making household with rule-driven counterparties:

1. Apply scheduled cashflows/events and assemble the claims due this month.
2. Expose those current books and due claims; call policy once.
3. Execute its submitted actions in order, stopping on the first unexecutable action.
4. If execution succeeds but any due claim remains unpaid, stop with that distinct cause.
5. Otherwise continue financial accrual/assessment and the next month's decision.

The first supported trade control declares immediate cash availability explicitly;
it does not claim actual products all settle immediately. Policies may call optional
funding helpers to ask how to satisfy their needs under supported product terms,
then compose the returned operations with payments and other actions. Helpers do
not execute those proposals, and the executor adds no funding or rebalancing pass.

The bounded example's `python_policy_test` pins the changed information boundary:
an opening $100 plus a current $100 contribution leads to a $200 chosen amount.
Its $30 due bill is paid before the $200 consumption action is rejected; $170 cash
remains and there is no retry. This is intentionally different from case A's
all-or-none group. Another synthetic case checks a $2.40 tax payment funded by the
same explicit sale/payment actions in compact and forensic capture.

```bash
bbr test //finance/augur/x/bounded_spending:python_policy_test
```

## Remaining choices

GP covers additional product/housing settlement and deadline semantics, and order
between multiple decision-making actors. Those extensions do not block the first
scoped example or introduce within-month callbacks/retries. Unsupported settlement
combinations must remain explicit gaps, not implicitly spendable proceeds.

Policy memory remains actor/path-local. Desired cuts and anchor changes are
intentions; paid amounts and completed transitions require actual receipts.
Record them separately without serializing arbitrary closure internals.

Configured timing controls do not imply that every existing housing/PE consumer
already runs through the action session; those migrations remain separate.
