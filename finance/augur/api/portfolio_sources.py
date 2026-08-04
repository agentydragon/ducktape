"""Resolve optional external portfolio sources into Augur's static runtime config."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime

from finance.augur.api.config import Config
from finance.augur.api.finance import FinanceSnapshot
from finance.augur.api.portfolio import (
    HoldingPositionConfig,
    HoldingTaxLotConfig,
    PortfolioAccountConfig,
    PortfolioConfig,
)
from finance.augur.api.portfolio_source_config import (
    FixedPortfolioSourceConfig,
    PlaidBalanceField,
    PlaidPortfolioSourceConfig,
    PlaidSp500ProxyGroupConfig,
)
from finance.augur.model.series import SP500_SYMBOL, SecurityKey
from finance.augur.sim.scenario import HarvestPolicy
from finance.plaid.db.read_model import (
    CurrentCashBalance,
    CurrentHolding,
    read_current_cash_balances,
    read_current_holdings,
)
from finance.plaid.db.schema import async_session_factory

logger = logging.getLogger(__name__)

# A direct-indexing sleeve's short-term harvest character comes from its recently-bought lots; a
# lot is short-term until it has been held a full year (IRC §1222), so buckets below this many
# months at month 0 contribute to the harvested loss's short-term share.
_SHORT_TERM_HOLDING_PERIOD_MONTHS = 12


@dataclass(frozen=True)
class _PortfolioContribution:
    cash_usd: float
    as_of_date: str | None
    accounts: tuple[PortfolioAccountConfig, ...]
    holdings: tuple[HoldingPositionConfig, ...]
    harvest_policies: tuple[HarvestPolicy, ...]
    latest_captured_at: datetime | None


@dataclass(frozen=True)
class ResolvedPortfolioSources:
    snapshot: FinanceSnapshot
    portfolio: PortfolioConfig
    # Reduced-form TLH harvest processes attached to index-tracking sleeves (Piece 2b). Empty when
    # no proxy group configures `harvest`. Fed into `Scenario.harvest_policies` so the engine's
    # `_apply_tlh_harvest` phase realizes calibrated losses with a sale-time basis give-back.
    harvest_policies: tuple[HarvestPolicy, ...]


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
        as_of_date=_merged_as_of_date(present), cash_usd=sum(contribution.cash_usd for contribution in present)
    )
    harvest_policies = tuple(policy for contribution in present for policy in contribution.harvest_policies)
    return ResolvedPortfolioSources(snapshot=snapshot, portfolio=portfolio, harvest_policies=harvest_policies)


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

    cash_usd = _cash_total(plaid, cash_balances)
    holdings_by_account: dict[str, list[CurrentHolding]] = {}
    for holding in current_holdings:
        holdings_by_account.setdefault(holding.account_id, []).append(holding)

    accounts: list[PortfolioAccountConfig] = []
    holdings: list[HoldingPositionConfig] = []
    harvest_policies: list[HarvestPolicy] = []
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
        holdings.append(_sp500_proxy_holding(group, group_holdings))
        if group.tlh_model is not None:
            harvest_policies.append(_harvest_policy(group))

    captured = [balance.captured_at for balance in cash_balances] + [
        holding.captured_at for holding in current_holdings
    ]
    return _PortfolioContribution(
        cash_usd=cash_usd,
        as_of_date=None,
        accounts=tuple(accounts),
        holdings=tuple(holdings),
        harvest_policies=tuple(harvest_policies),
        latest_captured_at=max(captured) if captured else None,
    )


def _harvest_policy(group: PlaidSp500ProxyGroupConfig) -> HarvestPolicy:
    """Build the scenario-level TLH harvest policy for one proxy group's sleeve.

    The policy is keyed to the proxy's (owner_agent, portfolio_account, SP500) lots — the same
    pool `_sp500_proxy_holding` expanded. The short-term share is the config override if set,
    else the buckets' short-term (<12mo) market-value share, else 1.0 (no buckets → young account,
    all short-term, matching the TY2025 1099-B).
    """

    assert group.tlh_model is not None  # caller guards
    tlh_model = group.tlh_model
    if tlh_model.short_term_fraction is not None:
        short_term_fraction = tlh_model.short_term_fraction
    else:
        short_term_fraction = _bucket_short_term_fraction(group)
    return HarvestPolicy(
        owner_agent_id=group.owner_agent_id,
        account_id=group.portfolio_account_id,
        asset=SecurityKey(symbol=SP500_SYMBOL),
        yield_params=tlh_model.yield_params,
        short_term_fraction=short_term_fraction,
    )


def _bucket_short_term_fraction(group: PlaidSp500ProxyGroupConfig) -> float:
    """Short-term (<12mo) market-value share across the proxy's holding-period buckets.

    With no buckets the sleeve is a single aggregate lot whose age is unknown; treat a fresh
    direct-indexing account as fully short-term (1.0), which both matches the TY2025 1099-B and is
    the conservative-for-deferral default (short-term losses are the more valuable to harvest)."""

    buckets = group.holding_period_buckets
    if not buckets:
        return 1.0
    total = sum(bucket.market_value_fraction for bucket in buckets)
    short_term = sum(
        bucket.market_value_fraction
        for bucket in buckets
        if bucket.holding_period_months_at_start < _SHORT_TERM_HOLDING_PERIOD_MONTHS
    )
    return short_term / total if total > 0.0 else 1.0


def _cash_total(plaid: PlaidPortfolioSourceConfig, balances: tuple[CurrentCashBalance, ...]) -> float:
    expected = set(plaid.cash.plaid_account_ids)
    actual = {balance.account_id for balance in balances}
    missing = sorted(expected - actual)
    if missing:
        raise ValueError(f"Plaid cash accounts have no current USD balance snapshot: {missing}")
    total = 0.0
    for balance in balances:
        value = balance.current if plaid.cash.balance_field == PlaidBalanceField.CURRENT else balance.available
        if value is None:
            raise ValueError(
                f"Plaid cash account {balance.account_id!r} has no {plaid.cash.balance_field.value} balance"
            )
        total += float(value)
    return total


def _sp500_proxy_holding(
    group: PlaidSp500ProxyGroupConfig, holdings: tuple[CurrentHolding, ...]
) -> HoldingPositionConfig:
    total_value_usd = 0.0
    total_cost_basis_usd = 0.0
    missing_basis: list[str] = []
    for holding in holdings:
        value = _holding_value_usd(holding)
        if value <= 0.0:
            continue
        total_value_usd += value
        if holding.cost_basis is None:
            missing_basis.append(holding.security_id)
        else:
            total_cost_basis_usd += float(holding.cost_basis)
    if total_value_usd <= 0.0:
        raise ValueError(f"Plaid SP500 proxy group {group.position_id!r} has no positive-value holdings")
    if missing_basis:
        logger.warning(
            "Plaid SP500 proxy group %s has holdings without cost basis; using zero basis for %s",
            group.position_id,
            sorted(missing_basis),
        )
    return HoldingPositionConfig(
        position_id=group.position_id,
        account_id=group.portfolio_account_id,
        label=group.label,
        symbol=group.symbol,
        security_kind=group.security_kind,
        value_series=SecurityKey(symbol=SP500_SYMBOL),
        unit_value_usd=float(group.unit_value_usd),
        lots=_proxy_lots(group, total_value_usd=total_value_usd, total_cost_basis_usd=total_cost_basis_usd),
    )


def _proxy_lots(
    group: PlaidSp500ProxyGroupConfig, *, total_value_usd: float, total_cost_basis_usd: float
) -> tuple[HoldingTaxLotConfig, ...]:
    unit_value_usd = float(group.unit_value_usd)
    if not group.holding_period_buckets:
        return (
            HoldingTaxLotConfig(
                lot_id=f"{group.position_id}_plaid_aggregate",
                holding_period_months_at_start=int(group.default_holding_period_months_at_start),
                quantity=total_value_usd / unit_value_usd,
                cost_basis_usd=total_cost_basis_usd,
            ),
        )
    # Distribute the live Plaid aggregate across the calibrated holding-period buckets. Normalize by
    # the configured fraction sums (validated to ~1.0) so the lot totals still equal the Plaid
    # snapshot exactly despite rounding in the authored fractions.
    buckets = group.holding_period_buckets
    market_value_fraction_sum = sum(bucket.market_value_fraction for bucket in buckets)
    basis_fractions = [bucket.cost_basis_fraction for bucket in buckets if bucket.cost_basis_fraction is not None]
    basis_fraction_sum = sum(basis_fractions) if basis_fractions else market_value_fraction_sum
    lots: list[HoldingTaxLotConfig] = []
    for bucket in buckets:
        market_value_weight = bucket.market_value_fraction / market_value_fraction_sum
        basis_weight = (
            bucket.cost_basis_fraction / basis_fraction_sum
            if bucket.cost_basis_fraction is not None
            else market_value_weight
        )
        lots.append(
            HoldingTaxLotConfig(
                lot_id=f"{group.position_id}_plaid_{bucket.key}",
                holding_period_months_at_start=int(bucket.holding_period_months_at_start),
                quantity=(total_value_usd * market_value_weight) / unit_value_usd,
                cost_basis_usd=total_cost_basis_usd * basis_weight,
            )
        )
    return tuple(lots)


def _holding_value_usd(holding: CurrentHolding) -> float:
    if holding.institution_value is not None:
        return float(holding.institution_value)
    if holding.quantity is not None and holding.institution_price is not None:
        return float(holding.quantity) * float(holding.institution_price)
    return 0.0


def _fixed_contribution(fixed: FixedPortfolioSourceConfig) -> _PortfolioContribution | None:
    if not fixed.enabled:
        return None
    return _PortfolioContribution(
        cash_usd=float(fixed.snapshot.cash_usd) if fixed.snapshot is not None else 0.0,
        as_of_date=fixed.snapshot.as_of_date if fixed.snapshot is not None else None,
        accounts=fixed.portfolio.accounts,
        holdings=fixed.portfolio.holdings,
        harvest_policies=(),
        latest_captured_at=None,
    )


def _merge_contributions(contributions: tuple[_PortfolioContribution, ...]) -> PortfolioConfig:
    accounts: list[PortfolioAccountConfig] = []
    holdings: list[HoldingPositionConfig] = []
    account_ids: set[str] = set()
    for contribution in contributions:
        for account in contribution.accounts:
            if account.account_id in account_ids:
                continue
            accounts.append(account)
            account_ids.add(account.account_id)
        holdings.extend(contribution.holdings)
    return PortfolioConfig(accounts=tuple(accounts), holdings=tuple(holdings))


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
