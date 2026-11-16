"""Shared format helpers for human and tmux renderers."""

from datetime import datetime, tzinfo
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from aiquota.models import PaceResult, PrepaidBalanceAvailable, QuotaWindow
from aiquota.pace import is_exhausted
from aiquota.peak_windows import BurnStatus, PeakInterval
from util.date_presentation import (
    format_clock as _shared_format_clock,
    format_instant,
    format_peak_interval as _shared_format_peak_interval,
)


def format_duration(seconds: float) -> str:
    s = max(0, round(seconds))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d > 0:
        return f"{d}d" + (f" {h}h" if h else "")
    if h > 0:
        return f"{h}h" + (f" {m}m" if m else "")
    return f"{m}m"


def format_window_duration(seconds: float) -> str:
    rounded = round(seconds)
    if rounded % 86400 == 0:
        return f"{rounded // 86400}d"
    if rounded % 3600 == 0:
        return f"{rounded // 3600}h"
    if rounded % 60 == 0:
        return f"{rounded // 60}m"
    return f"{rounded}s"


def format_window_label(window: QuotaWindow) -> str:
    duration = format_window_duration(window.window_seconds)
    return f"{window.name} ({duration})" if window.name else duration


def display_used_percent(window: QuotaWindow) -> int:
    """Usage as shown, capped below 100 until the window really is exhausted.

    Rounding 99.6% to "100%" would claim a window is spent while calls still go through, so
    only an exhausted window ever displays three digits.
    """
    rounded = round(window.used_percent)
    return rounded if is_exhausted(window) else min(rounded, 99)


def format_age(seconds: float) -> str:
    s = max(0, round(seconds))
    if s < 60:
        return f"{s}s"
    return format_duration(s)


def format_prepaid_balance(balance: PrepaidBalanceAvailable) -> str:
    """Format minor units without converting the provider balance to float."""
    return format_minor_currency(balance.amount_minor, balance.exponent, balance.currency)


def format_minor_currency(amount_minor: int, exponent: int, currency: str) -> str:
    """Format provider-reported minor units without converting to float."""
    sign = "-" if amount_minor < 0 else ""
    amount = Decimal(abs(amount_minor)).scaleb(-exponent)
    value = f"{amount:,.{exponent}f}"
    return f"{sign}${value}" if currency == "USD" else f"{sign}{value} {currency}"


def format_summary_minor_currency(amount_minor: int, exponent: int, currency: str) -> str:
    """Show provider precision below ten major units and whole amounts above."""
    precision = 2 if currency == "USD" else exponent
    return _format_summary_currency(Decimal(amount_minor).scaleb(-exponent), currency, precision)


def format_summary_usd(amount: Decimal | float) -> str:
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    return _format_summary_currency(value, "USD")


_HALF_CENT = Decimal("0.005")


def _round_currency(value: Decimal, precision: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)


def _format_summary_currency(value: Decimal, currency: str, precision: int = 2) -> str:
    sign = "-" if value < 0 else ""
    if currency == "USD" and value != 0 and abs(value) < _HALF_CENT:
        return f"{sign}<$0.01"
    if value == 0:
        digits = "0"
    elif abs(value) < 10:
        rounded = _round_currency(abs(value), precision)
        digits = f"{rounded:,.{precision}f}" if rounded != rounded.to_integral_value() else f"{rounded:,.0f}"
    else:
        digits = f"{abs(value).quantize(Decimal(1), rounding=ROUND_HALF_UP):,}"
    return f"{sign}${digits}" if currency == "USD" else f"{sign}{digits} {currency}"


def summary_hides_fraction(value: Decimal, precision: int = 2) -> bool:
    return abs(value) >= 10 and _round_currency(value, precision) != value.quantize(Decimal(1), rounding=ROUND_HALF_UP)


def same_at_precision(first: Decimal, second: Decimal, precision: int = 2) -> bool:
    return _round_currency(first, precision) == _round_currency(second, precision)


def format_exact_usd(amount: Decimal) -> str:
    if amount != 0 and abs(amount) < Decimal("1e-20"):
        return f"{'-' if amount < 0 else ''}${str(abs(amount).normalize()).lower()}"
    exponent = amount.as_tuple().exponent
    places = max(2, -exponent) if isinstance(exponent, int) else 2
    return f"{'-' if amount < 0 else ''}${abs(amount):,.{places}f}"


def format_extra_spend(used_usd: float, limit_usd: float) -> str:
    used, limit = Decimal(str(used_usd)), Decimal(str(limit_usd))
    used_label, limit_label = format_summary_usd(used), format_summary_usd(limit)
    qualifier = ""
    if not same_at_precision(used, limit) and used_label == limit_label:
        used_label, limit_label = _format_cents_usd(used), _format_cents_usd(limit)
    elif used != limit and used_label == limit_label:
        qualifier = f" · {'over' if used > limit else 'under'} by <$0.01"
    return f"Extra spend this month: {used_label} of {limit_label} limit{qualifier}"


def format_extra_spend_exact(used_usd: float, limit_usd: float) -> str:
    return f"{format_exact_usd(Decimal(str(used_usd)))} of {format_exact_usd(Decimal(str(limit_usd)))} limit"


def extra_spend_has_hidden_fraction(used_usd: float, limit_usd: float) -> bool:
    used, limit = Decimal(str(used_usd)), Decimal(str(limit_usd))
    if not same_at_precision(used, limit) and format_summary_usd(used) == format_summary_usd(limit):
        return False  # The summary expands values that would otherwise collide.
    return any(summary_hides_fraction(value) for value in (used, limit))


def _format_cents_usd(value: Decimal) -> str:
    return f"{'-' if value < 0 else ''}${abs(_round_currency(value, 2)):,.2f}"


def format_deadline(when: datetime, now: datetime, tz: tzinfo | None = None, *, details: bool = False) -> str:
    return format_instant(when, now=now, tz=tz, details=details) or ""


def format_reset_credit_summary(
    count: int,
    expiries: list[datetime],
    expiry_lookup_failed: bool,
    now: datetime,
    tz: tzinfo | None = None,
    *,
    details: bool = False,
) -> str:
    # The detail endpoint may be capped or lag the authoritative count.
    if count == 0:
        return "0"
    parts = [str(count)]
    known = sorted(expiries)
    if known:
        shown = known if details else known[:1]
        label = "known expiries" if details else "next known expiry"
        parts.append(f"{label} {', '.join(format_deadline(expiry, now, tz, details=details) for expiry in shown)}")
    if expiry_lookup_failed:
        parts.append("expiry lookup failed")
    elif count > len(known):
        missing = count - len(known)
        parts.append(f"{missing} expiry unknown" if missing == 1 else f"{missing} expiries unknown")
    return " · ".join(parts)


def format_paid_credit_balance(raw: str | None, unlimited: bool = False) -> str:
    if unlimited:
        return "unlimited credits"
    if raw is not None:
        try:
            amount = Decimal(raw)
            if amount.is_finite():
                return f"{amount.quantize(Decimal(1), rounding=ROUND_HALF_UP):,} credits"
        except InvalidOperation:
            pass
    return "available"


def format_pace(pace: PaceResult | None) -> str | None:
    if pace is None or not pace.stable:
        return None
    sign = "+" if pace.deviation >= 0 else "-"
    return f"{sign}{abs(round(pace.deviation))} pp"


def format_pace_forecast(pace: PaceResult | None, reset_seconds: float) -> str | None:
    if pace is None or not pace.stable or pace.projected_at_reset is None:
        return None
    projected = pace.projected_at_reset
    if projected > 100.5 and pace.seconds_to_exhaust is not None:
        shortfall = reset_seconds - pace.seconds_to_exhaust
        return f"exhausts ~{format_duration(shortfall)} before reset"
    if projected < 95:
        return f"leaves ~{round(100 - projected)}% unused at reset"
    return "on pace"


def format_local_clock(when: datetime, tz: tzinfo | None = None) -> str:
    """Render an instant as wall-clock time in `tz`, defaulting to system local.

    Vendors publish peak hours in their own zone and usually omit it; showing
    the viewer's clock is the whole point of surfacing this. The parameter
    exists so snapshot tests can pin a zone — without it, rendered output
    differs per machine and the snapshots only hold where they were recorded.
    """
    return _shared_format_clock(when, tz)


def format_multiplier(value: float) -> str:
    return f"{value:g}x"


def format_peak_interval(interval: PeakInterval, tz: tzinfo | None = None) -> str:
    """One upcoming window as `Day HH:MM-HH:MM` in `tz` (default system local)."""
    return _shared_format_peak_interval(interval.start, interval.end, tz)


def format_burn(burn: BurnStatus, now: datetime, tz: tzinfo | None = None) -> str:
    until = format_duration((burn.changes_at - now).total_seconds())
    if burn.in_peak:
        clock = format_local_clock(burn.changes_at, tz)
        return f"🔥 {format_multiplier(burn.multiplier)} burn until {clock} ({until}) — {burn.applies_to}"
    return (
        f"{format_multiplier(burn.multiplier)} burn — next {format_multiplier(burn.peak_multiplier)} window in {until}"
    )


def format_peak_schedule(burn: BurnStatus, tz: tzinfo | None = None) -> str | None:
    """Show the current peak and following windows in local time for planning."""
    if not burn.upcoming:
        return None
    windows = "  ".join(format_peak_interval(interval, tz) for interval in burn.upcoming)
    return f"{format_multiplier(burn.peak_multiplier)} windows (local): {windows}"
