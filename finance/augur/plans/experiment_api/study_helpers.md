# Ordinary experiment helpers

Design code, not an installed module. The other sketches import these definitions
as `study_helpers` to avoid repeating the same example. These are editable Python
functions, not new engine policy variants. A study can replace or ignore them.

```python
from collections.abc import Callable

from proposed_augur.actions import Action, Consume
from proposed_augur.instruments import Weights
from proposed_augur.markets import AnnualConvention
from proposed_augur.money import Money, RealAmount
from proposed_augur.observations import Observation
from proposed_augur.policies import Initialize, PolicyKey, Response, scalar_to_batch
from proposed_augur.proposals import Portfolio, PreviewAssumptions, preview, raise_cash, rebalance

IMMEDIATE = PreviewAssumptions(settlement="immediate")


def funded_consumption(
    obs: Observation, portfolio: Portfolio, amount: Money, *, assumptions: PreviewAssumptions,
) -> tuple[tuple[Action, ...], Observation | None]:
    sales = raise_cash(
        obs, portfolio, required_cash=amount, lot_order="fifo", assumptions=assumptions,
    )
    funded = preview(obs, sales, assumptions=assumptions)
    actions = sales + (Consume(portfolio.cash, "living", amount),)
    if funded.available_cash(portfolio.cash) < amount:
        # Keep the requested payment; no hidden cut. Execution stops at Consume.
        return actions, None
    return actions, preview(obs, actions, assumptions=assumptions)


def annual_policy(
    initial: RealAmount, portfolio: Portfolio, convention: AnnualConvention,
    *, budget: Callable[[Observation, float], float],
    target: Callable[[int], Weights], indexed: bool = True,
) -> Initialize:
    def decide(obs: Observation, previous: float) -> tuple[Response, float]:
        month = obs.month_index % 12
        actions: tuple[Action, ...] = ()
        projected = obs
        if month == convention.withdrawal_month:
            previous = budget(obs, previous)
            amount = (
                obs.nominal(RealAmount(previous, initial.basis)) if indexed
                else Money.from_number(previous, currency=initial.basis.currency)
            )
            actions, projected = funded_consumption(
                obs, portfolio, amount, assumptions=IMMEDIATE,
            )
            if projected is None:
                return Response(actions), previous
        if month == convention.rebalance_month and obs.months_remaining > 1:
            # Annual changes have already occurred at this month's observation.
            completed_years = obs.month_index // 12 + 1
            actions += rebalance(
                projected, portfolio, targets=target(completed_years),
                retain_cash=Money.from_number(0, currency=initial.basis.currency),
                lot_order="fifo", assumptions=IMMEDIATE,
            )
        return Response(actions), previous

    def initialize(key: PolicyKey):
        return decide, initial.value
    return scalar_to_batch(initialize)
```

The returned initializer supplies a batch policy through the optional scalar
adapter; the engine has no scalar callback. This helper withdraws once a year and
rebalances once a year; other monthly decisions
return no actions. When both occur in one month, this author's order is funding,
consumption, then rebalancing. Different paper conventions can use a different
function. There is no engine sorting or post-policy allocation pass.

For annual-only records, `annual_study_grid` places each annual gross return and
CPI change at one declared synthetic month's opening, leaving other months flat.
For example, withdrawal month 0, return month 11, rebalance month 11 means
withdraw → annual return → rebalance. Withdrawal month 11 instead means annual
return → withdraw → rebalance. These slots encode annual order, not realistic
intra-year returns; do not use this adapter for monthly spending or taxed products.
No terminal rebalance is requested. Every annual convention is recorded in output.

The funding helper never mutates a book. Preview uses the same financial
calculations as execution on actor-known inputs and explicit assumptions. An
unaffordable requested consumption remains unaffordable and fatal when submitted;
a preview is not a second policy call or an execution retry. Immediate settlement
and zero costs are explicit study assumptions, not promises for actual products.
