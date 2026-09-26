"""One private-equity issuer's protocol channels, as the integer paths a composed world reads.

A world holding a private lot requires all ten `private_equity_<channel>:<issuer>` series on
its path, so a suite that states a protocol states every channel. Each channel is authored
either as one value the whole horizon holds or as a value per snapshot, and lands on the grid
its kind uses: money in currency quanta, a dimensionless fraction in parts per billion, a
regime or event kind as its code, a flag as 0 or 1.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from finance.augur.model.series import PrivateEquityEventKindCode, PrivateEquityRegimeCode
from finance.augur.sim.fixed_point import currency_amount_to_quanta, rate_to_ppb
from finance.augur.sim.prepared import PreparedSeries

QUANTUM = Decimal("0.01")

type Money = Sequence[Decimal] | Decimal
type Rate = Sequence[float] | float
type Code = Sequence[int] | int


def _money(channel: Money, snapshots: int) -> tuple[int, ...]:
    values = [channel] * snapshots if isinstance(channel, Decimal) else list(channel)
    return tuple(int(currency_amount_to_quanta(value, quantum=QUANTUM)) for value in values)


def _rates(channel: Rate, snapshots: int) -> tuple[int, ...]:
    values = [channel] * snapshots if isinstance(channel, float) else list(channel)
    return tuple(rate_to_ppb(value) for value in values)


def _codes(channel: Code, snapshots: int) -> tuple[int, ...]:
    values = [channel] * snapshots if isinstance(channel, int) else list(channel)
    return tuple(int(value) for value in values)


def issuer_protocol(
    issuer_id: str,
    *,
    horizon_months: int,
    mark_usd: Money,
    regime: Code = PrivateEquityRegimeCode.PRIVATE_OPERATING,
    event_kind: Code = PrivateEquityEventKindCode.NONE,
    sale_opportunity: Code = 0,
    sale_capacity: Rate = 1.0,
    eligible: Rate = 1.0,
    forced_sale: Rate = 0.0,
    liquidity_blocked: Code = 0,
    forced_recovery_usd: Money = Decimal(0),
    company_valuation_usd: Money = Decimal(0),
) -> tuple[PreparedSeries, ...]:
    """The issuer's ten channels; every default is "nothing in the way"."""

    snapshots = horizon_months + 1
    channels = {
        "mark": _money(mark_usd, snapshots),
        "regime": _codes(regime, snapshots),
        "event_kind": _codes(event_kind, snapshots),
        "sale_opportunity": _codes(sale_opportunity, snapshots),
        "sale_capacity": _rates(sale_capacity, snapshots),
        "eligible": _rates(eligible, snapshots),
        "forced_sale": _rates(forced_sale, snapshots),
        "liquidity_blocked": _codes(liquidity_blocked, snapshots),
        "forced_recovery": _money(forced_recovery_usd, snapshots),
        "company_valuation": _money(company_valuation_usd, snapshots),
    }
    return tuple(
        PreparedSeries(series_id=f"private_equity_{channel}:{issuer_id}", snapshots=snapshots, values=values)
        for channel, values in channels.items()
    )


def at_month[T](value: T, *, month: int, default: T, snapshots: int) -> list[T]:
    """A channel holding `default` except in one month."""

    path = [default] * snapshots
    path[month] = value
    return path
