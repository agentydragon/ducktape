"""Command-line client for the current Plaid Spend view."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from typing import Any

from babel.numbers import format_currency, get_currency_precision
from dbus_next import Message
from dbus_next.aio import MessageBus
from dbus_next.constants import MessageType
from dbus_next.errors import DBusError

BUS_NAME = "works.allegedly.PlaidSpend"
OBJECT_PATH = "/works/allegedly/PlaidSpend"
INTERFACE_NAME = "works.allegedly.PlaidSpend1"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read your current Plaid statement-cycle spend.")
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


def _format_money(minor_units: Any, currency: Any) -> str:
    if not isinstance(minor_units, int) or isinstance(minor_units, bool):
        return "Unavailable"
    if not isinstance(currency, str) or len(currency) != 3:
        return f"{minor_units} minor units"

    code = currency.upper()
    precision = get_currency_precision(code)
    amount = Decimal(minor_units).scaleb(-precision)
    fraction = f".{'0' * precision}" if precision else ""
    currency_format = f"¤¤ #,##0{fraction}"
    return format_currency(amount, code, currency_format, locale="en_US", currency_digits=False)


def _card_title(card: dict[str, Any]) -> str:
    title = card.get("label") or card.get("account_name") or "Card"
    if mask := card.get("mask"):
        title += f" ···· {mask}"
    if institution := card.get("institution_name"):
        title += f" ({institution})"
    return title


def _print_view(view: dict[str, Any], status: str, last_error: str) -> None:
    cards = view["cards"]
    if not cards:
        print("No card data is available.")
        if status == "authentication-required":
            print("Sign in with `plaid-spend login` to load your view.")
        elif status != "ready":
            print(f"Desktop client: {status}.")
        if last_error:
            print(last_error)
        return

    print(f"Plaid Spend · {len(cards)} card{'s' if len(cards) != 1 else ''}")
    if generated_at := view.get("generated_at"):
        print(f"View updated: {generated_at}")

    for card in cards:
        currency = card.get("currency")
        print(f"\n{_card_title(card)}")
        if card.get("cycle_start"):
            print(f"  Statement cycle starts: {card['cycle_start']}")
        else:
            print("  Statement cycle: unavailable")

        spend = _format_money(card.get("spend_minor_units"), currency)
        limit = (
            "no limit set"
            if card.get("limit_minor_units") is None
            else _format_money(card.get("limit_minor_units"), currency)
        )
        percent = card.get("spend_percent")
        percent_text = f" · {percent:.1f}%" if isinstance(percent, int | float) else ""
        print(f"  Spend: {spend} / {limit}{percent_text}")

        posted = card.get("posted_minor_units")
        pending = card.get("pending_minor_units")
        if posted is not None or pending is not None:
            posted_text = _format_money(posted, currency) if posted is not None else "—"
            pending_text = _format_money(pending, currency) if pending is not None else "—"
            print(f"  Posted: {posted_text} · Pending: {pending_text}")

        alert = str(card.get("alert_state") or "unavailable").replace("_", " ").title()
        threshold = card.get("alert_threshold_percent")
        if alert == "Warning" and isinstance(threshold, int):
            alert += f" · {threshold}% threshold"
        print(f"  Alert: {alert}")
        print(f"  Last synced: {card.get('last_synced_at') or 'unknown'}")

    if status != "ready":
        print(f"\nDesktop client: {status}.")
        if last_error:
            print(last_error)


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
        else:
            _print_view(view, status, last_error)
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
