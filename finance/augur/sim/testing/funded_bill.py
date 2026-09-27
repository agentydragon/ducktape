"""A bill due at month zero that a cashless household funds by selling its one long-held lot.

Stipulated prices and a synthetic flat tax, not a forecast or statutory tax: even paths price the
stock at USD 100, so the sale pays the USD 150 bill and the tax on its gain is paid at month 12;
odd paths price it at USD 50, so the payment after the sale is rejected.
"""

from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.sim.actions import Action, DecisionActions, LotSale, PayClaim, Sell
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef
from finance.augur.sim.claims import ObligationType
from finance.augur.sim.external_series import ExternalSeriesContext, compile_series
from finance.augur.sim.fixed_point import quantity_scale_for_asset, quantity_to_quanta
from finance.augur.sim.ids import AccountId, AgentId, AssetId, JurisdictionId, LotId
from finance.augur.sim.income import ORDINARY_INCOME
from finance.augur.sim.jurisdictions import (
    Jurisdiction,
    JurisdictionLevel,
    StatutoryAmount,
    StatutoryIndexation,
    TaxBracket,
)
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.money import USD
from finance.augur.sim.observations import Decision
from finance.augur.sim.prepared import PreparedJurisdiction, PreparedObligation, PreparedSeries
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.tax_indexation import FixedNominalLaw
from finance.augur.sim.tax_profile import FilingStatus, TaxProfile, compile_profile
from finance.augur.sim.world import World

# Reaches the month-12 payment of the tax on the month-0 sale.
HORIZON = 13
CHECKING = AccountId("checking")
HOUSEHOLD = AgentId("example-household")
CREDITOR = AgentId("example-creditor")
TAX_AUTHORITY = AgentId("example-tax")
STOCK = SecurityKey(symbol=SecuritySymbol("example-stock"))
_FLAT_TAX = Jurisdiction(
    jurisdiction_id=JurisdictionId("example-flat-tax"),
    level=JurisdictionLevel.FEDERAL,
    ordinary_income_brackets={FilingStatus.SINGLE: [TaxBracket(upper="Infinity", rate=Decimal("0.20"))]},
    ltcg_brackets={FilingStatus.SINGLE: [TaxBracket(upper="Infinity", rate=Decimal("0.10"))]},
    standard_deduction={FilingStatus.SINGLE: Decimal(0)},
    max_capital_loss_ordinary_offset={FilingStatus.SINGLE: Decimal(0)},
    law_year=2024,
    indexation=dict.fromkeys(
        (
            StatutoryAmount.ORDINARY_INCOME_BRACKETS,
            StatutoryAmount.LTCG_BRACKETS,
            StatutoryAmount.STANDARD_DEDUCTION,
            StatutoryAmount.MAX_CAPITAL_LOSS_ORDINARY_OFFSET,
        ),
        StatutoryIndexation.FIXED,
    ),
)


@dataclass(frozen=True)
class Situation:
    """The stipulated price paths every composed world shares."""

    series: tuple[PreparedSeries, ...]
    rollout_count: int


def situation(rollout_count: int = 2) -> Situation:
    """USD 100 on even paths and USD 50 on odd ones, flat throughout: repeated stipulations, not market samples."""
    prices = np.repeat(np.resize([100.0, 50.0], rollout_count)[:, None], HORIZON + 1, axis=1)
    paths = ExternalSeriesContext.from_level_blocks(
        [(STOCK, prices)], rollout_count=rollout_count, horizon_months=HORIZON
    )
    return Situation(
        series=compile_series(paths, rollout_count=rollout_count, horizon_months=HORIZON, currency=USD),
        rollout_count=rollout_count,
    )


def compose(case: Situation, rollout_id: int) -> World:
    """No cash and two shares at USD 40 basis bought 24 months before month zero, with a USD 150 bill due at month 0.

    The creditor and tax authority are scripted sinks.
    """
    world = World(
        MarketPath(case.series, rollout_id, rollout_count=case.rollout_count),
        horizon_months=HORIZON,
        income_sources=(ORDINARY_INCOME,),
        jurisdictions=(PreparedJurisdiction(jurisdiction_id=_FLAT_TAX.jurisdiction_id, level=_FLAT_TAX.level),),
    )
    for agent_id in (HOUSEHOLD, CREDITOR, TAX_AUTHORITY):
        world.declare_account(account=AccountRef(agent_id=agent_id, account_id=CHECKING), opening_balance=0)
    profile = TaxProfile(
        agent_id=HOUSEHOLD,
        jurisdiction_ids=[_FLAT_TAX.jurisdiction_id],
        tax_authority_agent_id=TAX_AUTHORITY,
        prior_year_tax=Decimal(0),
    )
    world.track(
        TaxAuthority(
            compile_profile(profile, {_FLAT_TAX.jurisdiction_id: _FLAT_TAX}, currency=USD), indexation=FixedNominalLaw()
        )
    )
    scale = quantity_scale_for_asset(STOCK)
    world.declare_pool(agent_id=HOUSEHOLD, account_id=CHECKING, asset_id=AssetId(STOCK.symbol), quantity_scale=scale)
    world.hold_lot(
        lot_id=LotId("example-lot"),
        agent_id=HOUSEHOLD,
        account_id=CHECKING,
        asset_id=AssetId(STOCK.symbol),
        purchase_month=-24,
        quantity_scale=scale,
        units=quantity_to_quanta(2, scale=scale),
        basis=USD.quanta(80),
    )
    world.track(
        Biller(
            PreparedObligation(
                month=0,
                obligation_id="example-bill",
                obligation_type=ObligationType.OUTSIDE_RENT,
                from_account=AccountRef(agent_id=HOUSEHOLD, account_id=CHECKING),
                to_account=AccountRef(agent_id=CREDITOR, account_id=CHECKING),
                amount_due=USD.quanta(150),
                property_id=None,
                deduction_category=None,
                deductible_fraction_ppb=1_000_000_000,
            )
        )
    )
    return world


def sell_then_pay(batch: list[Decision]) -> list[DecisionActions]:
    """Sell every lot in a month whose claims exceed cash, then pay each claim in full."""
    responses = []
    for decision in batch:
        observation = decision.observation
        claims = observation.claims
        short = observation.cash < sum(claim.amount_due for claim in claims)
        actions: list[Action] = [
            Sell(
                cause_id=f"fund-{position.lot_id}",
                agent_id=observation.agent_id,
                proceeds_account_id=CHECKING,
                asset_id=position.asset_id,
                lots=(LotSale(account_id=position.account_id, lot_id=position.lot_id, units=position.units),),
            )
            for position in (observation.public_positions if short else ())
        ]
        actions.extend(
            PayClaim(
                request_id=request_id,
                cause_id=f"pay-{claim.cause_id}",
                claim=claim,
                from_account=claim.from_account,
                amount=claim.amount_due,
            )
            for request_id, claim in enumerate(claims)
        )
        responses.append(DecisionActions(rollout_id=decision.rollout_id, month=observation.month, actions=actions))
    return responses
