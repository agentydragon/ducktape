"""Real monthly action sessions for the product's sales-only funding policy.

Tests use stipulated prices/indexes and, where stated, a synthetic flat tax schedule.
The configured product runner remains a separate cutover caller, not a fallback here.
"""

from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import pytest_bazel

from finance.augur.api.config import DistributionTaxShareConfig, SecurityDistributionConfig
from finance.augur.api.portfolio import (
    HoldingTaxLotConfig,
    PortfolioAccountConfig,
    PortfolioConfig,
    SecurityHoldingConfig,
)
from finance.augur.model.series import LevelSeriesKey, SecurityDistributionKey, SecurityKey, SecuritySymbol
from finance.augur.product.funding import Policy
from finance.augur.product.holdings import opening_holdings
from finance.augur.product.scenarios import PRIMARY_ACCOUNT_ID, TAX_AUTHORITY_AGENT_ID, Situation, build_situation
from finance.augur.product.wire import FundingPolicy, ScenarioKey, SecuritySleeveWeight, SpendIndex
from finance.augur.sim.bills import Biller
from finance.augur.sim.external_series import ExternalSeriesContext, compile_series
from finance.augur.sim.ids import AccountId, AgentId, LotId
from finance.augur.sim.income import Taxable
from finance.augur.sim.jurisdictions import HYPOTHETICAL_FLAT_TAX, Jurisdiction, flat_income_tax
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.results import Rollout
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.tax_indexation import FixedNominalLaw
from finance.augur.sim.tax_profile import TaxProfile, compile_profile
from finance.augur.sim.testing.session import finish
from finance.augur.sim.world import World

BROKERAGE = AccountId("brokerage")

ACTOR = AgentId("test-owner")
FIRST = SecurityKey(symbol=SecuritySymbol("test-first"))


def lot(
    id_: LotId, account: AccountId, asset: SecurityKey, quantity: Decimal, month: int = -24
) -> SecurityHoldingConfig:
    """A position holding one lot bought `month` months from month 0."""
    return SecurityHoldingConfig(
        position_id=id_,
        account_id=account,
        symbol=asset.symbol,
        unit_value=Decimal(100),
        lots=(
            HoldingTaxLotConfig(
                lot_id=id_,
                holding_period_months_at_start=-month,
                quantity=float(quantity),
                cost_basis=quantity * Decimal(50),
            ),
        ),
    )


@dataclass(frozen=True)
class Product:
    """The app's situation for one request, and the opening portfolio the Python policy reads."""

    situation: Situation
    portfolio: PortfolioConfig


def product_situation(
    config: FundingPolicy,
    *,
    lots: tuple[SecurityHoldingConfig, ...],
    spend: Decimal = Decimal(10),
    horizon: int = 1,
    distributions: tuple[SecurityDistributionConfig, ...] = (),
) -> Product:
    portfolio = PortfolioConfig(
        accounts=tuple(
            PortfolioAccountConfig(account_id=account_id, owner_agent_id=ACTOR)
            for account_id in dict.fromkeys(position.account_id for position in lots)
        ),
        holdings=lots,
    )
    situation = build_situation(
        ScenarioKey(
            model_id="stipulated-policy-control",
            horizon_months=horizon,
            monthly_spend=spend,
            spend_index=SpendIndex.NONE,
            funding_policy=config,
        ),
        primary_agent_id=ACTOR,
        initial_cash=Decimal(0),
        holdings=opening_holdings(
            portfolio, distributions, tlh_portfolios=(), primary_agent_id=ACTOR, payout_account_id=PRIMARY_ACCOUNT_ID
        ),
        properties_by_id={},
        locations={},
    )
    return Product(situation=situation, portfolio=portfolio)


def run(
    product: Product,
    config: FundingPolicy,
    series: dict[LevelSeriesKey, np.ndarray],
    *,
    tax: tuple[TaxProfile, Jurisdiction] | None = None,
) -> Rollout:
    """The product's accounts, holdings and claims with explicit Python decisions in place of its household.

    Untaxed unless `tax` names a profile and its rule; the configured funding policy is not consulted.
    """
    situation = product.situation
    jurisdictions = {} if tax is None else {tax[1].jurisdiction_id: tax[1]}
    world = World(
        MarketPath(
            compile_series(
                ExternalSeriesContext.from_level_blocks(
                    list(series.items()), rollout_count=1, horizon_months=situation.horizon_months
                ),
                rollout_count=1,
                horizon_months=situation.horizon_months,
                currency=situation.currency,
            ),
            0,
            rollout_count=1,
        ),
        horizon_months=situation.horizon_months,
        income_sources=situation.income_sources,
    )
    for account, balance in situation.accounts:
        world.declare_account(account=account, opening_balance=balance)
    if tax is not None:
        world.track(
            TaxAuthority(
                compile_profile(tax[0], jurisdictions, currency=situation.currency), indexation=FixedNominalLaw()
            )
        )
    for pool in situation.pools:
        world.declare_pool(
            agent_id=pool.agent_id,
            account_id=pool.account_id,
            asset_id=pool.asset_id,
            quantity_scale=pool.quantity_scale,
        )
    for held in situation.lots:
        world.hold_lot(
            lot_id=held.lot_id,
            agent_id=held.agent_id,
            account_id=held.account_id,
            asset_id=held.asset_id,
            purchase_month=held.purchase_month,
            quantity_scale=held.quantity_scale,
            units=held.units,
            basis=held.basis,
        )
    for distribution in situation.distributions:
        world.declare_distribution(
            agent_id=distribution.agent_id,
            holding_account_id=distribution.holding_account_id,
            asset_id=distribution.asset_id,
            to_account_id=distribution.to_account_id,
            tax_character=distribution.tax_character,
        )
    for obligation in situation.obligations:
        world.track(
            Biller(
                obligation_id=obligation.obligation_id,
                obligation_type=obligation.obligation_type,
                from_account=obligation.from_account,
                to_account=obligation.to_account,
                amount_due=obligation.amount_due,
                property_id=obligation.property_id,
                deduction_category=obligation.deduction_category,
                deductible_fraction_ppb=obligation.deductible_fraction_ppb,
                schedule=obligation.schedule,
            )
        )
    policy = Policy(
        config,
        actor_id=ACTOR,
        cash_account_id=PRIMARY_ACCOUNT_ID,
        portfolio=product.portfolio,
        currency=situation.currency,
    )
    return finish(ActionSession({0: world}, ACTOR), policy).rollouts[0]


def test_coupon_precedes_funding_and_next_year_tax_is_an_explicit_funded_claim() -> None:
    config = FundingPolicy(sleeve_weights=(SecuritySleeveWeight(symbol=FIRST.symbol, weight=1),))
    rule = flat_income_tax(HYPOTHETICAL_FLAT_TAX, ordinary_rate=Decimal("0.20"), ltcg_rate=Decimal("0.10"))
    product = product_situation(
        config,
        spend=Decimal(50),
        horizon=13,
        lots=(lot(LotId("fund"), BROKERAGE, FIRST, Decimal(20)),),
        distributions=(
            SecurityDistributionConfig(
                symbol=FIRST.symbol, tax_character=(DistributionTaxShareConfig(fraction=1.0, character=Taxable()),)
            ),
        ),
    )
    profile = TaxProfile(
        agent_id=ACTOR,
        jurisdiction_ids=[rule.jurisdiction_id],
        tax_authority_agent_id=TAX_AUTHORITY_AGENT_ID,
        prior_year_tax=Decimal(0),
    )
    coupons = np.zeros((1, 14))
    coupons[:, 0] = 2
    result = run(
        product,
        config,
        {FIRST: np.full((1, 14), 100.0), SecurityDistributionKey(symbol=FIRST.symbol): coupons},
        tax=(profile, rule),
    )
    assert result.trace is not None
    sales = result.trace.events.lot_dispositions
    assert sales.select("proceeds_quanta", "cost_basis_consumed_quanta").row(0) == (1000, 500)
    # Year 1: $40 coupon × 20% + $280 realized LT gains × 10% = $36 tax.
    assert result.summary.tax_accruals[0].total_tax == 3600
    assert sum(row.amount_paid for row in result.summary.tax_payments) == 3600
    assert sales.select("month_index", "proceeds_quanta").row(-1) == (12, 8600)
    assert result.stop is None


if __name__ == "__main__":
    pytest_bazel.main()
