"""Command-line client for the current Plaid Spend view."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from babel.numbers import format_currency, get_currency_precision
from dbus_next import Message
from dbus_next.aio import MessageBus
from dbus_next.constants import MessageType
from dbus_next.errors import DBusError
from sparklines import sparklines

from finance.plaid.spend.allowance import AllowanceView, Status
from finance.plaid.spend.models import CardView, SpendView
from finance.plaid.spend.pace_history_models import PaceHistory

BUS_NAME = "works.allegedly.PlaidSpend"
OBJECT_PATH = "/works/allegedly/PlaidSpend"
INTERFACE_NAME = "works.allegedly.PlaidSpend1"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read your current Plaid card spend and flexible allowance.")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("show", "status", "login"),
        default="show",
        help="show the current view (default), check the desktop client, or start sign-in",
    )
    parser.add_argument("--json", action="store_true", help="print the view as JSON")
    parser.add_argument("--details", action="store_true", help="show the full allowance and card details")
    parser.add_argument("--pace-window", choices=("7d", "30d"), default="7d", help="rolling average (default: 7d)")
    parser.add_argument(
        "--history-days", type=int, choices=(7, 14, 30), default=30, help="sparkline span (default: 30)"
    )
    parser.add_argument(
        "--scale", choices=("zero", "auto"), default="zero", help="sparkline lower bound (default: zero)"
    )
    parser.add_argument("--no-sparkline", action="store_true", help="omit the pace history")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto", help="sparkline color")
    return parser


async def _call(bus: MessageBus, member: str, *, interface: str = INTERFACE_NAME) -> list[Any]:
    reply = await bus.call(Message(destination=BUS_NAME, path=OBJECT_PATH, interface=interface, member=member))
    if reply is None:
        raise RuntimeError(f"Plaid Spend D-Bus call {member} returned no reply")
    if reply.message_type == MessageType.ERROR:
        detail = str(reply.body[0]) if reply.body else reply.error_name
        if reply.error_name == "org.freedesktop.DBus.Error.ServiceUnknown":
            raise RuntimeError("the Plaid Spend desktop service is not running in this user session")
        raise RuntimeError(detail)
    return reply.body


async def _property(bus: MessageBus, name: str) -> str:
    reply = await bus.call(
        Message(
            destination=BUS_NAME,
            path=OBJECT_PATH,
            interface=PROPERTIES_INTERFACE,
            member="Get",
            signature="ss",
            body=[INTERFACE_NAME, name],
        )
    )
    if reply is None:
        raise RuntimeError(f"Plaid Spend D-Bus property {name} returned no reply")
    if reply.message_type == MessageType.ERROR:
        detail = str(reply.body[0]) if reply.body else reply.error_name
        raise RuntimeError(detail)
    value = reply.body[0].value
    if not isinstance(value, str):
        raise RuntimeError(f"Plaid Spend returned an invalid {name} property")
    return value


def _format_money(minor_units: int | None, currency: str | None) -> str:
    if minor_units is None:
        return "Unavailable"
    if not isinstance(currency, str) or len(currency) != 3:
        return f"{minor_units} minor units"

    code = currency.upper()
    precision = get_currency_precision(code)
    amount = Decimal(minor_units).scaleb(-precision)
    digits = precision if amount and abs(amount) < 10 and amount != amount.to_integral_value() else 0
    pattern = "¤#,##0" + ("." + "0" * digits if digits else "")
    rounded = amount.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    return format_currency(rounded, code, pattern, locale="en_US", currency_digits=False)


def _card_title(card: CardView) -> str:
    title = card.label or card.account_name or "Card"
    if mask := card.mask:
        title += f" ···· {mask}"
    if institution := card.institution_name:
        title += f" ({institution})"
    return title


def _format_timestamp(value: datetime | None, *, now: datetime | None = None, tz: tzinfo | None = None) -> str:
    if value is None:
        return "unknown"
    current = (now or datetime.now().astimezone()).astimezone(tz)
    local = value.astimezone(tz)
    label = local.strftime("%b %-d")
    if local.year != current.year:
        label += local.strftime(", %Y")
    if abs(local.astimezone(UTC) - current.astimezone(UTC)) <= timedelta(days=14):
        label += local.strftime(", %H:%M")
    return label


def _format_allowance(allowance: AllowanceView) -> str:
    lines = ["", "Flexible allowance · advisory, not a bank limit"]
    if allowance.status != Status.ACTIVE:
        lines.append(f"  Status: {allowance.status}")
        if allowance.note:
            lines.append(f"  {allowance.note}")
        return "\n".join(lines)

    currency = allowance.currency
    lines.append(f"  Available: {_format_money(allowance.available_minor_units, currency)}")
    lines.append(f"  Monthly credit: {_format_money(allowance.monthly_minor_units, currency)}")
    spent = next(
        (report.spend_minor_units for report in allowance.spend_periods if report.period.id == "credit_cycle"), None
    )
    lines.append(f"  Spent this credit cycle: {_format_money(spent, currency)}")
    lines.append(f"  Pending (included): {_format_money(allowance.pending_minor_units, currency)}")
    lines.append(f"  Provisional leash signal: {allowance.spending_signal.replace('_', ' ')}")
    for period_id, label, short_label in (("rolling_7d", "7-day", "7d"), ("rolling_30d", "30-day", "30d")):
        report = next((report for report in allowance.recorded_pace_periods if report.period.id == period_id), None)
        lines.append(
            f"  {label} recorded flexible pace: "
            f"{_format_money(report.observed_daily_minor_units if report else None, currency)}/day"
        )
        unmatched = report.unmatched_charges if report else None
        if unmatched and unmatched.count:
            lines.append(
                f"  {short_label} unmatched: {unmatched.count} ({_format_money(unmatched.amount_minor_units, currency)})"
            )
    lines.append(f"  Provisional leash rate: ~{_format_money(allowance.daily_reference_minor_units, currency)}/day")
    lines.append("  Leash capacity is not a sustainability target; unmatched purchases count as flexible.")
    lines.append("  History before activation informs pace but not the available balance.")
    lines.append(f"  Forecast basis: {allowance.forecast.basis_period.id.replace('_', ' ')}")
    lines.append(f"  Forecast signal: {allowance.forecast.alert_state.replace('_', ' ')}")
    lines.append(
        f"  Estimated balance before next credit: {_format_money(allowance.forecast.projected_cycle_end_minor_units, currency)}"
    )
    lines.append(f"  Next credit: {_format_timestamp(allowance.next_credit_at)}")
    exhaustion = (
        _format_timestamp(allowance.forecast.estimated_exhaustion_at)
        if allowance.forecast.estimated_exhaustion_at
        else "no recent spend"
    )
    lines.append(f"  Projected exhaustion (no future credits): {exhaustion}")
    lines.append(f"  Oldest account sync: {_format_timestamp(allowance.last_synced_at)}")
    if allowance.note:
        lines.append(f"  {allowance.note}")
    return "\n".join(lines)


def _format_view(view: SpendView, status: str, last_error: str) -> str:
    lines: list[str] = []
    cards = view.cards
    if view.allowance:
        lines.append(_format_allowance(view.allowance))
        if view.dashboard_url:
            lines.append(f"  Check a purchase / dashboard: {view.dashboard_url}")
    if not cards:
        lines.append("No card data is available.")
        if status == "authentication-required":
            lines.append("Sign in with `plaid-spend login` to load your view.")
        elif status != "ready":
            lines.append(f"Desktop client: {status}.")
        if last_error:
            lines.append(last_error)
        return "\n".join(lines)

    lines.append(f"Plaid Spend · {len(cards)} card{'s' if len(cards) != 1 else ''}")
    lines.append(f"View updated: {_format_timestamp(view.generated_at)}")

    for card in cards:
        currency = card.currency
        lines.extend(["", _card_title(card)])
        if card.statement_period.kind == "statement":
            lines.append(f"  Statement cycle starts: {card.statement_period.start.isoformat()}")
        elif card.statement_period.kind == "provisional":
            lines.append(
                f"  Since first recorded transaction: {card.statement_period.start.isoformat()} (statement date unavailable)"
            )
        else:
            lines.append("  Statement cycle: unavailable")

        spend = _format_money(card.spend_minor_units, currency)
        limit = "no limit set" if card.limit_minor_units is None else _format_money(card.limit_minor_units, currency)
        percent = card.spend_percent
        percent_text = f" · {percent:.1f}%" if percent is not None else ""
        lines.append(
            f"  Spend: {spend} / {limit}{percent_text}"
            if card.statement_period.kind == "statement"
            else f"  Recorded spend: {spend}"
        )

        posted = card.posted_minor_units
        pending = card.pending_minor_units
        if posted is not None or pending is not None:
            posted_text = _format_money(posted, currency) if posted is not None else "—"
            pending_text = _format_money(pending, currency) if pending is not None else "—"
            lines.append(f"  Posted: {posted_text} · Pending: {pending_text}")

        alert = str(card.alert_state or "unavailable").replace("_", " ").title()
        threshold = card.alert_threshold_percent
        if alert == "Warning" and isinstance(threshold, int):
            alert += f" · {threshold}% threshold"
        lines.append(f"  Alert: {alert}")
        lines.append(f"  Last synced: {_format_timestamp(card.last_synced_at)}")

    if status != "ready":
        lines.extend(["", f"Desktop client: {status}."])
        if last_error:
            lines.append(last_error)
    return "\n".join(lines)


def _sparkline(history: PaceHistory, window: str, days: int, scale: str, color: bool) -> str:
    selected = next((item for item in history.windows if item.period_id == f"rolling_{window}"), None)
    if selected is None:
        raise ValueError("history window unavailable")
    points = selected.points[-days:]
    values = [point.observed_daily_minor_units for point in points]
    values = [value if value is not None and value >= 0 else None for value in values]
    present = [value for value in values if value is not None]
    if len(present) < 2:
        return "Pace history lacks coverage"
    lower = 0 if scale == "zero" else min(present)
    upper = max(present)
    reference = history.daily_reference_minor_units
    chars = []
    graph = sparklines(values, minimum=lower, maximum=upper)[0]
    for value, char in zip(values, graph, strict=True):
        if value is None:
            chars.append("·")
            continue
        if color and reference > 0:
            code = 31 if value * 5 > reference * 6 else 33 if value > reference else 32
            chars.append(f"\x1b[{code}m{char}\x1b[0m")
        else:
            chars.append(char)
    range_label = f"{_format_money(lower, history.currency)} to {_format_money(upper, history.currency)}/day"
    gaps = " · gaps: unknown" if len(present) != len(values) else ""
    return f"{window} pace · last {len(values)} days: {''.join(chars)}  ({range_label}{gaps})"


def _allowance_summary(allowance: AllowanceView, window: str, history_line: str | None) -> list[str]:
    if allowance.status != Status.ACTIVE:
        return [f"Allowance: {allowance.status}", *([allowance.note] if allowance.note else [])]

    lines = [f"Available: {_format_money(allowance.available_minor_units, allowance.currency)} · advisory allowance"]
    report = next((p for p in allowance.recorded_pace_periods if p.period.id == f"rolling_{window}"), None)
    observed = report.observed_daily_minor_units if report else None
    reference = allowance.daily_reference_minor_units
    pace = f"{_format_money(observed, allowance.currency)}/day" if observed is not None else "unavailable"
    ratio = (
        f" · {round(observed * 100 / reference)}% of leash"
        if observed is not None and reference and reference > 0
        else ""
    )
    lines.append(f"{window} pace: {pace}{ratio} · leash ~{_format_money(reference, allowance.currency)}/day")
    if observed is not None and reference is not None and reference > 0:
        if observed * 5 > reference * 6:
            lines.append("Pace over 120% of leash")
        elif observed > reference:
            lines.append("Pace above leash")
    if report and report.unmatched_charges and report.unmatched_charges.count:
        unmatched = report.unmatched_charges
        lines.append(
            f"Unmatched: {unmatched.count} ({_format_money(unmatched.amount_minor_units, allowance.currency)})"
        )
    if history_line:
        lines.append(history_line)
    return lines


def _format_summary(view: SpendView, status: str, last_error: str, window: str, history_line: str | None) -> str:
    lines = ["Plaid Spend"]
    if view.allowance:
        lines.extend(_allowance_summary(view.allowance, window, history_line))
    for card in view.cards:
        spend = _format_money(card.spend_minor_units, card.currency)
        if card.statement_period.kind == "statement":
            limit = (
                _format_money(card.limit_minor_units, card.currency)
                if card.limit_minor_units is not None
                else "no limit"
            )
            percent = f" · {card.spend_percent:.1f}%" if card.spend_percent is not None else ""
            lines.append(f"{_card_title(card)}: {spend} / {limit}{percent}")
        else:
            lines.append(f"{_card_title(card)}: recorded {spend} · statement cycle unavailable")
        if card.alert_state and card.alert_state not in ("normal", "unavailable"):
            lines.append(f"  Alert: {str(card.alert_state).replace('_', ' ')}")
    if not view.cards:
        lines.append("No card data is available.")
    lines.append(f"View updated: {_format_timestamp(view.generated_at)}")
    if status != "ready":
        lines.append(f"Desktop client: {status}.")
        if status == "authentication-required":
            lines.append("Sign in with `plaid-spend login`.")
        if last_error:
            lines.append(last_error)
    return "\n".join(lines)


async def _history_line(bus: MessageBus, window: str, days: int, scale: str, color: bool) -> str:
    try:
        result = await _call(bus, "GetPaceHistory")
        if not result or not isinstance(result[0], str):
            raise ValueError("invalid history reply")
        history = PaceHistory.model_validate_json(result[0])
        return _sparkline(history, window, days, scale, color)
    except RuntimeError, ValueError, DBusError:
        return "Pace history unavailable"


async def _run(
    command: str,
    json_output: bool,
    *,
    details: bool = False,
    window: str = "7d",
    history_days: int = 30,
    scale: str = "zero",
    show_sparkline: bool = True,
    color: bool = False,
) -> None:
    bus = await MessageBus().connect()
    try:
        if command == "login":
            await _call(bus, "Login")
            print("Sign-in started in your browser. Finish the Authentik flow, then run `plaid-spend`.")
            return

        status = await _property(bus, "Status")
        last_error = await _property(bus, "LastError")
        if command == "status":
            print(f"Status: {status}")
            if last_error:
                print(f"Last error: {last_error}")
            return

        result = await _call(bus, "GetView")
        if not result or not isinstance(result[0], str):
            raise RuntimeError("Plaid Spend returned an invalid view")
        view = json.loads(result[0])
        if not isinstance(view, dict) or not isinstance(view.get("cards"), list):
            raise RuntimeError("Plaid Spend returned an invalid view; expected an object with a cards array")
        if json_output:
            print(json.dumps(view, indent=2, ensure_ascii=False))
            return
        if view.get("generated_at") is None:
            print("No card data is available.")
            print(f"Desktop client: {status}.")
            if last_error:
                print(last_error)
            return
        parsed = SpendView.model_validate(view)
        if details:
            print(_format_view(parsed, status, last_error))
            return
        history_line = None
        if show_sparkline and status == "ready" and parsed.allowance and parsed.allowance.status == Status.ACTIVE:
            history_line = await _history_line(bus, window, history_days, scale, color)
        print(_format_summary(parsed, status, last_error, window, history_line))
    finally:
        bus.disconnect()


def main() -> None:
    args = _parser().parse_args()
    try:
        color = args.color == "always" or (
            args.color == "auto" and sys.stdout.isatty() and "NO_COLOR" not in os.environ
        )
        asyncio.run(
            _run(
                args.command,
                args.json,
                details=args.details,
                window=args.pace_window,
                history_days=args.history_days,
                scale=args.scale,
                show_sparkline=not args.no_sparkline,
                color=color,
            )
        )
    except (DBusError, OSError, RuntimeError, ValueError) as exc:
        print(f"plaid-spend: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
