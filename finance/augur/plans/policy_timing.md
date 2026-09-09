# Policy timing: executable cases and remaining choices

Scoped timing contract and remaining **GP** choices in [the landing plan](roadmap.md).
The tests below exercise current
canonical execution, not the target [actor-facing interface](policy_interfaces.md).
Economic agency defines that boundary; scoped timing/execution choices remain open.

## Name the cashflows, not just “spending”

Reporting terms (`consumption_requested` / `consumption_paid` are native summary fields):

| Term                    | Meaning                                                                                  | Not equivalent to                                                                                    |
| ----------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `asset_sale_proceeds`   | Gross cash raised by asset sales; separate fees when modeled.                            | Realized gain, consumption, or all cash available.                                                   |
| `consumption_requested` | Demand for an identified consumption component; here, the policy's discretionary budget. | A sale order, tax-inclusive funding requirement, or an unchosen lifestyle anchor.                    |
| `consumption_paid`      | The amount settled for that same demand.                                                 | All non-tax outflows: purchases, principal repayments and own-account transfers are not consumption. |
| `tax_paid`              | Cash paid to taxing authorities, with refunds/prepayments separately identifiable.       | Tax accrued, taxable income, or money spent on the chosen lifestyle.                                 |
| `contract_payments`     | Payments due under existing commitments, with their own categories.                      | Entirely consumption: rent and interest differ from loan principal.                                  |

For the current spending hook, `consumption_requested` is its return value and
`consumption_paid` is that demand's `ObligationOutcome.amount_paid`. Summary capture
tracks the actual demand, independently of configured claims' chosen cause IDs.
A policy requesting zero emits no obligation row but has an observed zero in the
compact series; post-stop months are absent. Do not derive
consumption by subtracting taxes from sale proceeds or summing all transfers to
an outside actor. A broader lifestyle-consumption measure must explicitly combine
policy consumption with eligible contract categories without counting principal
or double-counting rent already included in a budget.

These examples use nominal USD cents, one supplied deterministic path, constant
prices/CPI, zero cash bands, cashflow-only allocation and no fees, distributions,
borrowing or housing assets. The figures are synthetic accounting controls, not
forecast results. The tax example's flat rates are not a jurisdiction's statutes.

## Case A: a deliberate cut versus an unfunded request

`rust/engine/tests.rs::policy_timing_guardrail_and_unpaid_consumption` starts with
$100 cash and $1,000 of sellable stock; an existing $700 rent demand is due in
month 0. The policy's consumption component excludes rent; rent is also household
consumption, despite being committed. Taxes are explicitly absent. Compare a $500 request with an executable
rule that cuts it to $300 when opening gross wealth is below $1,200.

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
The cut is a different decision before funding, not an automatic response invented
by settlement. Failed paths receive no future spending callbacks; existing tests
also cover deliberate zero requests and stopping after failure.

## Case B: consumption, tax payment and surplus investment

`rust/engine/tests.rs::policy_timing_surplus_investment_reserves_tax_and_consumption` starts
from the same $100 cash and $1,000 stock, with $500 basis. No rent. The synthetic
tax profile charges long-term gains at 10%, ordinary income at 20%, no deduction,
and no prior-year tax target. Compare investing surplus versus leaving it in cash
using today's `allow_purchases` choice. It is **not** a within-path target-allocation
callback. Native allocation's separate tax-and-consumption case exercises a
changed target with the same demand-reservation semantics.

| Event month | Observation/decision                                              | Execution                                                                                                                                       |
| ----------- | ----------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| 0           | Policy sees $100 cash/$1,000 stock and requests $500 consumption. | Sell $400, using $200 basis and realizing $200 gain. Pay $500 consumption. No tax payment yet.                                                  |
| 1–10        | Policy sees $0 cash/$600 stock; requests zero.                    | No additional sales or consumption.                                                                                                             |
| 11          | Same observation and zero request.                                | Accrue $20 tax on the $200 gain at year-end. Accrual is not a cash payment.                                                                     |
| 12          | Policy still sees $0 cash/$600 stock and requests $50.            | A supplied $100 nontaxable contribution arrives after the observation. Pay $50 consumption and $20 tax; only the remaining $30 can be invested. |

Both arms pay $550 consumption and $20 tax. With reinvestment enabled the last
snapshot has a new $30-basis lot and no cash; otherwise it has $30 cash and no new
lot. Payment timing here is **current synthetic engine behavior**, not a claim
that January true-up represents statutory filing/payment deadlines. The
[tax checklist](tax_coverage.md) tracks that gap separately.

The current engine phase order is: observe opening holdings at current prices → apply current
cashflows/events → assemble demands → funding sales → grouped settlement → surplus
purchases → year-end assessment when due. A failed group stops subsequent
purchases, but is not a rollback of every action earlier in the month.

Run the two cases without network evidence downloads:

```bash
bbr test //finance/augur/rust:simulator_test --test_arg=policy_timing_
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

Current opening-month/all-or-none cases remain controls for P6. Moving strategy out
of the engine does not preserve their old information boundary: the new action
policy must see the cashflows and claims whose funding it now chooses.

## Remaining choices

GP covers additional product/housing settlement and deadline semantics, and order
between multiple decision-making actors. Those extensions do not block the first
scoped example or introduce within-month callbacks/retries. Unsupported settlement
combinations must remain explicit gaps, not implicitly spendable proceeds.

Policy memory remains actor/path-local. Desired cuts and anchor changes are
intentions; paid amounts and completed transitions require actual receipts.
Record them separately without serializing arbitrary closure internals.

The existing timing controls do not implement the new actor loop,
zero-target/full-exit arithmetic, compact allocation capture or explicit
decision-transition receipts.
