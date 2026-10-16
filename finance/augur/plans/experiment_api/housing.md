# Housing and investments: several actors, one household policy

Proposed Python, not runnable. Compare renting and buying over the same joint
market paths, varying supplied financing offers and ordinary investment functions.
This preserves the housing use case independently of the old web app.

```python
from collections.abc import Callable
from dataclasses import dataclass
from itertools import product

import polars as pl

from proposed_augur.accounting import Actor
from proposed_augur.actions import Action, AcceptLease, PurchaseHome
from proposed_augur.contracts import LeaseOffer, PurchaseOffer
from proposed_augur.data import NamedSeries
from proposed_augur.markets import MarketModel
from proposed_augur.money import Money
from proposed_augur.observations import Observation
from proposed_augur.policies import Initialize, PolicyKey, Response, ScalarInitialize, scalar_to_batch
from proposed_augur.proposals import (
    Portfolio, PreviewAssumptions, PreviewError, preview, raise_cash,
)
from proposed_augur.results import Observer, Runs, StudyResult, financial_observers
from proposed_augur.simulation import run as run_paths
from proposed_augur.state import Situation

# These functions belong to the experiment. Closing costs are read from supplied
# offers using canonical calculations, not re-derived tax/mortgage arithmetic.
type CashToAccept = Callable[[Observation, LeaseOffer | PurchaseOffer], Money]
def housing_policy(
    offer: LeaseOffer | PurchaseOffer, portfolio: Portfolio,
    cash_to_accept: CashToAccept, monthly: ScalarInitialize, assumptions: PreviewAssumptions,
) -> Initialize:
    def initialize(key: PolicyKey):
        continue_month, initial_memory = monthly(key)

        def decide(obs: Observation, memory: object):
            opening: tuple[Action, ...] = ()
            projected = obs
            if obs.month_index == 0:
                opening = raise_cash(
                    obs, portfolio, required_cash=cash_to_accept(obs, offer),
                    lot_order="fifo", assumptions=assumptions,
                )
                acceptance = (
                    AcceptLease(portfolio.cash, offer) if isinstance(offer, LeaseOffer)
                    else PurchaseHome(portfolio.cash, offer)
                )
                opening += (acceptance,)
                try:
                    projected = preview(obs, opening, assumptions=assumptions)
                except PreviewError:
                    return Response(opening), memory
            # A normal Python function composition within this one monthly call,
            # not a second engine observation or callback after executing a purchase.
            response, memory = continue_month(projected, memory)
            return Response(opening + response.actions), memory

        return decide, initial_memory
    return scalar_to_batch(initialize)


@dataclass(frozen=True)
class Inputs:
    situation: Situation
    household_id: str
    market_model: MarketModel
    observations: NamedSeries
    offers: dict[str, LeaseOffer | PurchaseOffer]
    portfolio: Portfolio
    cash_to_accept: CashToAccept
    monthly_policies: dict[str, ScalarInitialize]
    assumptions: PreviewAssumptions
    study_observers: dict[str, Observer]
    years: int
    paths: int


def housing(inputs: Inputs) -> StudyResult:
    household = inputs.situation.actor(inputs.household_id)
    # Supplied offers must reference these same counterparties and their accounts.
    situation = inputs.situation.with_actors(
        Actor.external("mortgage_lender"), Actor.external("property_seller"),
        Actor.external("landlord"),
    )
    worlds = inputs.market_model.condition(inputs.observations, at=situation.as_of).sample(
        years=inputs.years, step="month", paths=inputs.paths, seed=731,
    )
    rows: list[pl.DataFrame] = []
    runs: Runs = {}
    for (housing_name, offer), (policy_name, monthly) in product(
        inputs.offers.items(), inputs.monthly_policies.items(),
    ):
        initialize = housing_policy(
            offer, inputs.portfolio, inputs.cash_to_accept, monthly, inputs.assumptions,
        )
        run = run_paths(
            situation, policies={household: initialize}, reporting_actor=household, worlds=worlds,
            observers=financial_observers(
                "terminal_wealth_real", "terminal_liquid_wealth_real", "housing_cost_real",
                "tax_paid_real", "mortgage_balance_real",
            ) | inputs.study_observers,
        )
        rows.append(run.paths.select(
            (~pl.col("reached_horizon")).mean().alias("stopped_fraction"),
            pl.col("contract_default").mean().alias("default_fraction"),
            pl.col("purchase_completed").mean().alias("purchase_fraction"),
            pl.col("terminal_wealth_real").filter(pl.col("reached_horizon"))
            .median().alias("median_terminal_completed"),
            pl.col("terminal_liquid_wealth_real").filter(pl.col("reached_horizon"))
            .quantile(0.05).alias("p05_liquid_completed"),
        ).with_columns(housing=pl.lit(housing_name), policy=pl.lit(policy_name)))
        runs[housing_name, policy_name] = run
    return pl.concat(rows), runs
```

`monthly_policies` contains scalar authoring helpers for coordinated
spending/claim-payment/investment; the complete housing function is adapted once
to the canonical batch-only API. These are functions
such as the [personal example](spending_allocation.md), not separate engine-owned
spending and allocation hooks. The author may vary target trajectories, reserve
rules and consumption without changing mortgage accounting. Newly created
obligations and remaining cash appear in the preview used to compose the ordered
list, and actual receipts arrive at the next monthly call.

Purchase offers contain seller terms, closing costs and any actual lender offer:
principal, rate, term, eligibility and collateral. Acceptance is validated; the
policy does not construct a loan to declare credit granted. The source of any
required closing cash is explicit. Lease offers include rent/reset, exit/renewal
terms and dates. Carrying costs and tax-relevant attributes belong to the property.
A joint model supplies compatible investment, home-price and local-rent paths;
these products cannot be inferred from a generic equity/bond forecast.

External actors have balancing accounts/claims and supplied contractual behavior,
not invented household tax profiles. Only the household gets this decision
function; a later lender-risk experiment can model lender decisions separately.
Reporting is explicitly household-scoped: a lender receivable or seller's cash
cannot inflate the household's wealth. The mortgage must reconcile across books.

An unaffordable or ineligible purchase is a fatal action for its rollout, not a
silent fallback to renting or another financing attempt. A successful funding sale
before the failed purchase remains in the trace. Purchase failure and later
mortgage default are distinct; the observer records actual completion, and the rent
arm's purchase fraction is not applicable (null).

This example retains the house at the horizon and reports its value net of debt;
it does not sell for free. Explicit sale-date/cost variants require supported
property-sale actions. Moving cannot erase the mortgage. Monthly housing claims
are paid explicitly by the policy and excluded from nonhousing consumption.
Property valuation, transaction basis and statutory closing-cost treatment must
share the canonical financial rules; this API sketch is not evidence they are
all implemented or correct.
