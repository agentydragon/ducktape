"""Command-line client for the current Plaid Spend view."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from babel.numbers import format_currency, get_currency_precision
from dbus_next import Message
from dbus_next.aio import MessageBus
from dbus_next.constants import MessageType
from dbus_next.errors import DBusError

from finance.plaid.spend.allowance import AllowanceView, Status
from finance.plaid.spend.models import CardView, SpendView

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
    currency_format = "¤¤ #,##0"
    if amount and abs(amount) < Decimal("0.5"):
        whole = format_currency(1, code, currency_format, locale="en_US", currency_digits=False)
        return f"{'-' if amount < 0 else ''}<{whole}"
    rounded = amount.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return format_currency(rounded, code, currency_format, locale="en_US", currency_digits=False)


def _card_title(card: CardView) -> str:
    title = card.label or card.account_name or "Card"
    if mask := card.mask:
        title += f" ···· {mask}"
    if institution := card.institution_name:
        title += f" ({institution})"
    return title


def _format_timestamp(value: datetime | None) -> str:
    return value.isoformat().replace("+00:00", "Z") if value else "unknown"


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
    daily_reference = round(allowance.monthly_minor_units * 12 / 365.2425)
    lines.append(f"  Provisional leash rate: ~{_format_money(daily_reference, currency)}/day")
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
        percent_text = f" · {percent:.1f}%" if isinstance(percent, int | float) else ""
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


async def _run(command: str, json_output: bool) -> None:
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
        elif view.get("generated_at") is None:
            print("No card data is available.")
            print(f"Desktop client: {status}.")
            if last_error:
                print(last_error)
        else:
            print(_format_view(SpendView.model_validate(view), status, last_error))
    finally:
        bus.disconnect()


def main() -> None:
    args = _parser().parse_args()
    try:
        asyncio.run(_run(args.command, args.json))
    except (DBusError, OSError, RuntimeError, ValueError) as exc:
        print(f"plaid-spend: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
