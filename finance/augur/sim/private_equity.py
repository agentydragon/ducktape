"""Configured issuer protocol: exact recovery cashouts, compulsory sales and tender floors."""

from collections.abc import Sequence
from dataclasses import dataclass

from finance.augur.model.series import PrivateEquityEventKindCode, PrivateEquityRegimeCode
from finance.augur.sim.accounting import Accounting
from finance.augur.sim.actions import Sell
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE, quantity_for_value
from finance.augur.sim.holdings import Holdings, private_issuer
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.money import checked_count, mul_div, mul_div_wide, position_value
from finance.augur.sim.observations import TlhPortfolioObservation
from finance.augur.sim.prepared import PreparedScenario


@dataclass(frozen=True)
class ProtocolEvent:
    month: int
    issuer_id: str
    asset_id: str
    event_kind: str
    regime: str
    mark: int
    sale_capacity_fraction_ppb: int
    eligible_fraction_ppb: int
    forced_sale_fraction_ppb: int
    liquidity_blocked: bool
    forced_recovery_cashout: int


@dataclass(frozen=True)
class Opportunity:
    month: int
    cause_id: str
    issuer_id: str
    asset_id: str
    event_kind: str
    regime: str
    outcome: str
    mark: int
    sale_capacity_fraction_ppb: int
    eligible_fraction_ppb: int
    liquidity_blocked: bool
    floor: int
    liquid_net_worth: int
    shortfall: int
    quantity_scale: int
    units_held: int
    sellable_units: int
    target_units: int
    proceeds: int


def units_held(holdings: Holdings, candidates: Sequence[int]) -> int:
    total = 0
    for index in candidates:
        total = checked_count(total + holdings.lots[index].units_remaining, "private-equity units held")
    return total


def sellable_units(units: int, capacity: int, eligible: int) -> int:
    return checked_count(
        mul_div_wide(units, capacity * eligible, MONEY_FACTOR_SCALE**2, "private-equity sellable quantity"),
        "private-equity sellable quantity",
    )


def liquid_net_worth(
    scenario: PreparedScenario,
    accounting: Accounting,
    holdings: Holdings,
    market: MarketPath,
    marks: Sequence[TlhPortfolioObservation],
    actor: str,
    month: int,
) -> int:
    total = 0
    for account in scenario.accounts:
        if account.account.agent_id == actor:
            total = checked_count(total + accounting.ledger.balance(account.account), "money addition")
    for lot in holdings.lots:
        if lot.spec.agent_id != actor or private_issuer(lot.spec.asset_id) is not None or lot.units_remaining <= 0:
            continue
        series = f"security:{lot.spec.asset_id}"
        # The configured PE floor omits an unmarked public holding; it does not price bonds or property.
        if series not in market.series:
            continue
        total = checked_count(
            total + position_value(market.value(series, month), lot.units_remaining, lot.spec.quantity_scale),
            "money addition",
        )
    for mark in marks:
        if mark.owner_agent_id == actor:
            total = checked_count(total + mark.value, "money addition")
    return total


class PrivateEquity:
    def __init__(self) -> None:
        self.events: list[ProtocolEvent] = []
        self.opportunities: list[Opportunity] = []

    def advance(
        self,
        scenario: PreparedScenario,
        accounting: Accounting,
        holdings: Holdings,
        market: MarketPath,
        marks: Sequence[TlhPortfolioObservation],
        month: int,
    ) -> None:
        issuers = sorted({issuer for lot in holdings.lots if (issuer := private_issuer(lot.spec.asset_id)) is not None})
        for issuer in issuers:
            asset = f"private_equity:{issuer}"
            candidates = sorted(
                (index for index, lot in enumerate(holdings.lots) if lot.spec.asset_id == asset),
                key=lambda index: (holdings.lots[index].spec.purchase_month, holdings.lots[index].spec.lot_id),
            )
            first = holdings.lots[candidates[0]].spec
            actor, scale = first.agent_id, first.quantity_scale
            mark = market.value(f"private_equity_mark:{issuer}", month)
            regime = PrivateEquityRegimeCode(market.value(f"private_equity_regime:{issuer}", month))
            event = PrivateEquityEventKindCode(market.value(f"private_equity_event_kind:{issuer}", month))
            tender = market.value(f"private_equity_sale_opportunity:{issuer}", month) == 1
            capacity = market.value(f"private_equity_sale_capacity:{issuer}", month)
            eligible = market.value(f"private_equity_eligible:{issuer}", month)
            forced = market.value(f"private_equity_forced_sale:{issuer}", month)
            blocked = market.value(f"private_equity_liquidity_blocked:{issuer}", month) == 1
            recovery = market.value(f"private_equity_forced_recovery:{issuer}", month)
            if event != PrivateEquityEventKindCode.NONE:
                self.events.append(
                    ProtocolEvent(
                        month,
                        issuer,
                        asset,
                        event.name.lower(),
                        regime.name.lower(),
                        mark,
                        capacity,
                        eligible,
                        forced,
                        blocked,
                        recovery,
                    )
                )
            held = units_held(holdings, candidates)
            policy = next(
                (policy for policy in scenario._private_equity_tender_policies if policy.owner_agent_id == actor), None
            )
            if policy is None:
                if tender:
                    self.opportunities.append(
                        Opportunity(
                            month,
                            f"pe_opportunity_m{month}_{issuer}",
                            issuer,
                            asset,
                            event.name.lower(),
                            regime.name.lower(),
                            "no_policy",
                            mark,
                            capacity,
                            eligible,
                            blocked,
                            0,
                            0,
                            0,
                            scale,
                            held,
                            sellable_units(held, capacity, eligible),
                            0,
                            0,
                        )
                    )
                continue
            if recovery > 0 and held > 0:
                request = Sell(
                    cause_id=f"pe_forced_recovery_m{month}_{issuer}",
                    agent_id=actor,
                    proceeds_account_id=policy.proceeds_account_id,
                    asset_id=asset,
                    lots=holdings.fifo(candidates, held),
                )
                holdings.cashout(accounting, month, request, total=recovery)
            after_recovery = units_held(holdings, candidates)
            if forced > 0 and mark > 0 and after_recovery > 0:
                target = min(
                    after_recovery,
                    mul_div(after_recovery, forced, MONEY_FACTOR_SCALE, "private-equity forced-sale quantity"),
                )
                if target > 0:
                    request = Sell(
                        cause_id=f"pe_forced_sale_m{month}_{issuer}",
                        agent_id=actor,
                        proceeds_account_id=policy.proceeds_account_id,
                        asset_id=asset,
                        lots=holdings.fifo(candidates, target),
                    )
                    holdings.sell(accounting, month, request, price=mark)
            floor = market.amount(policy.liquid_net_worth_floor, month)
            liquid = liquid_net_worth(scenario, accounting, holdings, market, marks, actor, month)
            shortfall = max(0, checked_count(floor - liquid, "money subtraction"))
            held = units_held(holdings, candidates)
            sellable = sellable_units(held, capacity, eligible)
            shortfall_units = quantity_for_value(shortfall, mark, scale, round_up=True) if mark > 0 else 0
            public = regime == PrivateEquityRegimeCode.PUBLIC_MARKET
            active = (tender or public) and not blocked and mark > 0
            target = min(shortfall_units, sellable) if active else 0
            outcome = "sold"
            if shortfall <= 0:
                outcome = "floor_satisfied"
            if capacity == 0 or eligible == 0:
                outcome = "capacity_zero"
            if mark <= 0:
                outcome = "nonpositive_mark"
            if blocked:
                outcome = "liquidity_blocked"
            if held <= 0:
                outcome = "no_units"
            if tender:
                self.opportunities.append(
                    Opportunity(
                        month,
                        f"pe_opportunity_m{month}_{issuer}",
                        issuer,
                        asset,
                        event.name.lower(),
                        regime.name.lower(),
                        outcome,
                        mark,
                        capacity,
                        eligible,
                        blocked,
                        floor,
                        liquid,
                        shortfall,
                        scale,
                        held,
                        sellable,
                        target,
                        position_value(mark, target, scale),
                    )
                )
            if target > 0:
                cause = f"pe_public_market_m{month}_{issuer}" if public else f"pe_tender_m{month}_{issuer}"
                request = Sell(
                    cause_id=cause,
                    agent_id=actor,
                    proceeds_account_id=policy.proceeds_account_id,
                    asset_id=asset,
                    lots=holdings.fifo(candidates, target),
                )
                holdings.sell(accounting, month, request, price=mark)
