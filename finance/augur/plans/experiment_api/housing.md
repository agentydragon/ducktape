# Housing and investments: a distributional experiment with several agents

Proposed Python. Compare renting with buying, varying financing terms and the
investment policy over the same joint market paths. This preserves the useful
housing experiment independently of any particular web app. All prices, rents,
offers, local rules, and household details are supplied inputs.

```python
from dataclasses import dataclass
from itertools import product

import polars as pl

from proposed_augur.contracts import FixedRateMortgage, Lease, MortgageOffer, PropertyPurchase
from proposed_augur.instruments import Home, InvestableUniverse
from proposed_augur.markets import MarketModel
from proposed_augur.accounting import Actor
from proposed_augur.simulation import simulate
from proposed_augur.state import Situation
from proposed_augur.policies import HousingDecision, InvestmentPolicy, SpendingPolicy, Strategy
from proposed_augur.data import NamedSeries
from proposed_augur.results import Runs, StudyResult, financial_observers, ever_accepted_tag


@dataclass(frozen=True)
class Inputs:
    situation: Situation
    household_id: str
    market_model: MarketModel
    observations: NamedSeries
    universe: InvestableUniverse
    home: Home
    lease: Lease
    purchase: PropertyPurchase
    mortgage_offers: tuple[MortgageOffer, ...]
    investments: tuple[InvestmentPolicy, ...]
    nonhousing_spending: SpendingPolicy
    years: int
    paths: int


def housing(inputs: Inputs) -> StudyResult:
    household = inputs.situation.actor(inputs.household_id)
    lender = Actor.external("mortgage_lender")
    seller = Actor.external("property_seller")
    landlord = Actor.external("landlord")
    situation = inputs.situation.with_actors(lender, seller, landlord)
    # Already bound to these investments, the home, and local rent dynamics.
    market = inputs.market_model.condition(inputs.observations, at=situation.as_of)
    worlds = market.sample(
        years=inputs.years,
        step="month", paths=inputs.paths, seed=731,
    )

    housing_choices = {
        "rent": HousingDecision.lease(
            inputs.lease, tenant=household, landlord=landlord
        )
    }
    for offer in inputs.mortgage_offers:
        housing_choices[offer.name] = HousingDecision.buy(
            inputs.purchase,
            home=inputs.home, buyer=household, seller=seller,
            financing=FixedRateMortgage(
                offer=offer, borrower=household, lender=lender,
                collateral=inputs.home,
            ),
            disposition="retain_at_horizon",
            accepted_tag="home.purchase",
        )

    rows: list[pl.DataFrame] = []
    runs: Runs = {}
    for (housing_name, decision), investment in product(
        housing_choices.items(), inputs.investments
    ):
        strategy = Strategy(
            spending=inputs.nonhousing_spending,
            trading=investment,
            actions=(decision,),
        )
        run = simulate(
            situation, policies={household: strategy}, worlds=worlds, on_shortfall="stop",
            reporting_actor=household,
            observers=financial_observers(
                "terminal_wealth_real", "terminal_liquid_wealth_real",
                "total_spending_real", "housing_cost_real", "tax_paid_real",
                "mortgage_balance_real",
            ) | {"purchase_completed": ever_accepted_tag("home.purchase")},
        )
        rows.append(
            run.paths.select(
                pl.col("contract_default").mean().alias("default_fraction"),
                pl.col("unfunded_withdrawal").mean().alias("spending_shortfall_fraction"),
                pl.col("reached_horizon").mean().alias("completed_fraction"),
                (pl.lit(None) if housing_name == "rent" else pl.col("purchase_completed").mean())
                .alias("purchase_completed_fraction"),
                pl.col("terminal_wealth_real").filter(pl.col("reached_horizon"))
                .median().alias("median_terminal_wealth_completed"),
                pl.col("terminal_liquid_wealth_real").filter(pl.col("reached_horizon"))
                .quantile(0.05).alias("p05_liquid_wealth_completed"),
            ).with_columns(housing=pl.lit(housing_name), investment=pl.lit(investment.name))
        )
        runs[housing_name, investment.name] = run

    return pl.concat(rows), runs
```

`PropertyPurchase` specifies the execution date, price rule, closing costs,
down payment and occupancy. `MortgageOffer` supplies rate, principal/term and
eligibility conditions. `Lease` supplies dates, rent/reset terms and exit or
renewal behavior. The home supplies carrying-cost and tax-relevant attributes;
the joint model binds home prices and local rent alongside financial markets.
These inputs must cover the horizon or explicitly schedule their replacement.

The experiment assumes credit is available on the supplied offers and counterparties
honor their contracts. External actors maintain balancing accounts and claims but
do not optimize or receive an invented personal tax profile. A study of lender
risk could replace the external lender with a modeled actor without rewriting
mortgage arithmetic.

All outcome measures above belong to `household`, which must be the situation's
explicit reporting actor. Their wealth excludes the lender's receivable and the
seller's cash. The same loan creates a household liability and a lender claim;
origination, payment, interest, and payoff must remain visible on both books.

Buying spends cash and can require taxable sales. It creates enforceable future
payments. The investment policy acts on the remaining assets. A purchase that
cannot close is recorded as an unexecuted decision, distinct from a later mortgage
default. The purchase event observer is false when no purchase completed. The
summary marks the rent arm's purchase fraction not applicable (null); a failed
purchase in a buy arm is false, not successful buying.

`retain_at_horizon` reports home equity net of debt; it does not sell the house for
free. A disposition experiment can add explicit sale-date and cost variants. A
decision to move must settle or retain the mortgage according to its actual
property action. Monthly housing payments belong to contracts and are excluded
from `nonhousing_spending`.
