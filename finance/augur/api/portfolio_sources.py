"""Resolve optional external portfolio sources into Augur's static runtime config."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from finance.augur.api.config import Config
from finance.augur.api.finance import FinanceSnapshot
from finance.augur.api.portfolio import (
    BondHoldingConfig,
    HoldingPositionConfig,
    HoldingTaxLotConfig,
    PortfolioAccountConfig,
    PortfolioConfig,
    SecurityHoldingConfig,
)
from finance.augur.api.portfolio_source_config import (
    FixedPortfolioSourceConfig,
    PlaidBalanceField,
    PlaidPortfolioSourceConfig,
    PlaidSp500ProxyGroupConfig,
)
from finance.augur.model.series import SP500_SYMBOL, SecurityKey
from finance.augur.sim.scenario import TlhPortfolioSpec
from finance.plaid.db.read_model import (
    CurrentCashBalance,
    CurrentHolding,
    read_current_cash_balances,
    read_current_holdings,
)
from finance.plaid.db.schema import async_session_factory

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PortfolioContribution:
    cash: Decimal
    as_of_date: str | None
    accounts: tuple[PortfolioAccountConfig, ...]
    holdings: tuple[HoldingPositionConfig, ...]
    # Bonds ride the merge alongside holdings so `_merge_contributions` re-validates them as
    # part of one `PortfolioConfig`. Plaid contributes none: it imports positions, not terms.
    bonds: tuple[BondHoldingConfig, ...]
    tlh_portfolios: tuple[TlhPortfolioSpec, ...]
    latest_captured_at: datetime | None


@dataclass(frozen=True)
class ResolvedPortfolioSources:
    snapshot: FinanceSnapshot
    portfolio: PortfolioConfig
    tlh_portfolios: tuple[TlhPortfolioSpec, ...]


def resolve_portfolio_sources(config: Config) -> ResolvedPortfolioSources:
    """Materialize enabled portfolio sources into Augur's static runtime portfolio.

    v0 resolves Plaid at startup so all requests in one API process see a consistent initial
    portfolio snapshot.
    """

    contributions = [_fixed_contribution(config.portfolio_sources.fixed)]
    plaid = config.portfolio_sources.plaid
    if plaid.enabled:
        db_url = os.environ.get(plaid.database_url_env)
        if not db_url:
            raise ValueError(f"Plaid portfolio source is enabled but ${plaid.database_url_env} is not set")
        contributions.append(asyncio.run(_read_plaid_contribution(plaid, db_url=db_url)))
    present = tuple(contribution for contribution in contributions if contribution is not None)
    portfolio = _merge_contributions(present)
    snapshot = FinanceSnapshot(
        as_of_date=_merged_as_of_date(present),
        cash=sum((contribution.cash for contribution in present), start=Decimal(0)),
    )
    tlh_portfolios = tuple(policy for contribution in present for policy in contribution.tlh_portfolios)
    return ResolvedPortfolioSources(snapshot=snapshot, portfolio=portfolio, tlh_portfolios=tlh_portfolios)


async def _read_plaid_contribution(plaid: PlaidPortfolioSourceConfig, *, db_url: str) -> _PortfolioContribution:
    # One-shot resolve at startup: build a throwaway engine for `db_url`, read, then dispose it
    # (the budget path instead reuses one engine across requests). read_model now takes the
    # session factory rather than the url.
    engine, session_factory = async_session_factory(db_url)
    try:
        cash_balances = await read_current_cash_balances(
            session_factory=session_factory,
            account_ids=plaid.cash.plaid_account_ids,
            iso_currency_code=plaid.iso_currency_code,
        )
        group_account_ids = tuple(
            sorted({account_id for group in plaid.sp500_proxy_groups for account_id in group.plaid_account_ids})
        )
        current_holdings = await read_current_holdings(
            session_factory=session_factory, account_ids=group_account_ids, iso_currency_code=plaid.iso_currency_code
        )
    finally:
        await engine.dispose()

    cash = _cash_total(plaid, cash_balances)
    holdings_by_account: dict[str, list[CurrentHolding]] = {}
    for holding in current_holdings:
        holdings_by_account.setdefault(holding.account_id, []).append(holding)

    accounts: list[PortfolioAccountConfig] = []
    holdings: list[HoldingPositionConfig] = []
    tlh_portfolios: list[TlhPortfolioSpec] = []
    for group in plaid.sp500_proxy_groups:
        group_holdings = tuple(
            holding for account_id in group.plaid_account_ids for holding in holdings_by_account.get(account_id, ())
        )
        if not group_holdings:
            raise ValueError(f"Plaid SP500 proxy group {group.position_id!r} has no current holdings")
        accounts.append(
            PortfolioAccountConfig(
                account_id=group.portfolio_account_id,
                owner_agent_id=group.owner_agent_id,
                account_type=group.account_type,
                label=group.account_label,
            )
        )
        proxy_holding = _sp500_proxy_holding(group, group_holdings)
        holdings.append(proxy_holding)
        if group.tlh_assumptions is not None:
            opening = PortfolioConfig(accounts=(accounts[-1],), holdings=(proxy_holding,))
            tlh_portfolios.append(
                TlhPortfolioSpec(
                    portfolio_id=group.position_id,
                    owner_agent_id=group.owner_agent_id,
                    account_id=group.portfolio_account_id,
                    asset=SecurityKey(symbol=SP500_SYMBOL),
                    initial_lots=list(opening.to_initial_lots()),
                    assumptions=group.tlh_assumptions,
                )
            )

    captured = [balance.captured_at for balance in cash_balances] + [
        holding.captured_at for holding in current_holdings
    ]
    return _PortfolioContribution(
        cash=cash,
        as_of_date=None,
        accounts=tuple(accounts),
        holdings=tuple(holdings),
        # Plaid imports positions, not bond terms — a coupon rate and a maturity are not in the
        # holdings feed, so a bond ladder is deployment-authored config only.
        bonds=(),
        tlh_portfolios=tuple(tlh_portfolios),
        latest_captured_at=max(captured) if captured else None,
    )


def _cash_total(plaid: PlaidPortfolioSourceConfig, balances: tuple[CurrentCashBalance, ...]) -> Decimal:
    expected = set(plaid.cash.plaid_account_ids)
    actual = {balance.account_id for balance in balances}
    missing = sorted(expected - actual)
    if missing:
        raise ValueError(f"Plaid cash accounts have no current USD balance snapshot: {missing}")
    total = Decimal(0)
    for balance in balances:
        value = balance.current if plaid.cash.balance_field == PlaidBalanceField.CURRENT else balance.available
        if value is None:
            raise ValueError(
                f"Plaid cash account {balance.account_id!r} has no {plaid.cash.balance_field.value} balance"
            )
        total += Decimal(str(value))
    return total


def _sp500_proxy_holding(
    group: PlaidSp500ProxyGroupConfig, holdings: tuple[CurrentHolding, ...]
) -> HoldingPositionConfig:
    total_value = Decimal(0)
    total_cost_basis = Decimal(0)
    missing_basis: list[str] = []
    for holding in holdings:
        value = _holding_value(holding)
        if value <= 0:
            continue
        total_value += value
        if holding.cost_basis is None:
            missing_basis.append(holding.security_id)
        else:
            total_cost_basis += Decimal(str(holding.cost_basis))
    if total_value <= 0:
        raise ValueError(f"Plaid SP500 proxy group {group.position_id!r} has no positive-value holdings")
    if missing_basis:
        logger.warning(
            "Plaid SP500 proxy group %s has holdings without cost basis; using zero basis for %s",
            group.position_id,
            sorted(missing_basis),
        )
    # The sleeve IS the S&P series by definition (that is what a "SP500 proxy group" means), so
    # its symbol is the index symbol, not whichever ticker the brokerage happens to hold. The
    # configured ticker survives as the display label.
    return SecurityHoldingConfig(
        position_id=group.position_id,
        account_id=group.portfolio_account_id,
        label=group.label or group.symbol,
        symbol=SP500_SYMBOL,
        security_kind=group.security_kind,
        unit_value=group.unit_value,
        lots=_proxy_lots(group, total_value=total_value, total_cost_basis=total_cost_basis),
    )


def _proxy_lots(
    group: PlaidSp500ProxyGroupConfig, *, total_value: Decimal, total_cost_basis: Decimal
) -> tuple[HoldingTaxLotConfig, ...]:
    unit_value = group.unit_value
    if not group.holding_period_buckets:
        return (
            HoldingTaxLotConfig(
                lot_id=f"{group.position_id}_plaid_aggregate",
                holding_period_months_at_start=int(group.default_holding_period_months_at_start),
                quantity=float(total_value / unit_value),
                cost_basis=total_cost_basis,
            ),
        )
    # Distribute the live Plaid aggregate across the calibrated holding-period buckets. Normalize by
    # the configured fraction sums (validated to ~1.0) so the lot totals still equal the Plaid
    # snapshot exactly despite rounding in the authored fractions.
    buckets = group.holding_period_buckets
    market_value_fractions = [Decimal(str(bucket.market_value_fraction)) for bucket in buckets]
    market_value_fraction_sum = sum(market_value_fractions, start=Decimal(0))
    basis_fractions = [
        Decimal(str(bucket.cost_basis_fraction)) for bucket in buckets if bucket.cost_basis_fraction is not None
    ]
    basis_fraction_sum = sum(basis_fractions) if basis_fractions else market_value_fraction_sum
    lots: list[HoldingTaxLotConfig] = []
    for bucket, market_value_fraction in zip(buckets, market_value_fractions, strict=True):
        market_value_weight = market_value_fraction / market_value_fraction_sum
        basis_weight = (
            Decimal(str(bucket.cost_basis_fraction)) / basis_fraction_sum
            if bucket.cost_basis_fraction is not None
            else market_value_weight
        )
        lots.append(
            HoldingTaxLotConfig(
                lot_id=f"{group.position_id}_plaid_{bucket.key}",
                holding_period_months_at_start=int(bucket.holding_period_months_at_start),
                quantity=float((total_value * market_value_weight) / unit_value),
                cost_basis=total_cost_basis * basis_weight,
            )
        )
    return tuple(lots)


def _holding_value(holding: CurrentHolding) -> Decimal:
    if holding.institution_value is not None:
        return Decimal(str(holding.institution_value))
    if holding.quantity is not None and holding.institution_price is not None:
        return Decimal(str(holding.quantity)) * Decimal(str(holding.institution_price))
    return Decimal(0)


def _fixed_contribution(fixed: FixedPortfolioSourceConfig) -> _PortfolioContribution | None:
    if not fixed.enabled:
        return None
    return _PortfolioContribution(
        cash=fixed.snapshot.cash if fixed.snapshot is not None else Decimal(0),
        as_of_date=fixed.snapshot.as_of_date if fixed.snapshot is not None else None,
        accounts=fixed.portfolio.accounts,
        holdings=fixed.portfolio.holdings,
        bonds=fixed.portfolio.bonds,
        tlh_portfolios=(),
        latest_captured_at=None,
    )


def _merge_contributions(contributions: tuple[_PortfolioContribution, ...]) -> PortfolioConfig:
    accounts: list[PortfolioAccountConfig] = []
    holdings: list[HoldingPositionConfig] = []
    bonds: list[BondHoldingConfig] = []
    account_ids: set[str] = set()
    for contribution in contributions:
        for account in contribution.accounts:
            if account.account_id in account_ids:
                continue
            accounts.append(account)
            account_ids.add(account.account_id)
        holdings.extend(contribution.holdings)
        bonds.extend(contribution.bonds)
    return PortfolioConfig(accounts=tuple(accounts), holdings=tuple(holdings), bonds=tuple(bonds))


def _merged_as_of_date(contributions: tuple[_PortfolioContribution, ...]) -> str:
    values: list[date] = []
    fallback: str | None = None
    for contribution in contributions:
        if contribution.as_of_date is not None:
            fallback = contribution.as_of_date
            with contextlib.suppress(ValueError):
                values.append(date.fromisoformat(contribution.as_of_date))
        if contribution.latest_captured_at is not None:
            values.append(contribution.latest_captured_at.date())
    if values:
        return max(values).isoformat()
    if fallback is not None:
        return fallback
    raise ValueError("resolved portfolio sources did not provide an as_of_date")
