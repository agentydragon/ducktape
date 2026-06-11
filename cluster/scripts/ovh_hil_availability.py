"""List sub-$100/month OVH dedicated servers available in Hillsboro."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

OVH_API = "https://api.us.ovhcloud.com/1.0"
DEFAULT_CATALOGS = ("eco", "baremetalServers")


@dataclass(frozen=True)
class ServerRow:
    price: str
    monthly_usd: float
    plan: str
    name: str
    cpu: str
    memory: str
    storage: str
    availability: str
    orderable_now: bool


def get_json(path: str) -> Any:
    with urllib.request.urlopen(f"{OVH_API}{path}", timeout=60) as response:
        return json.load(response)


def monthly_pricing(plan: dict[str, Any]) -> tuple[float, str] | None:
    candidates = [
        pricing
        for pricing in plan.get("pricings", [])
        if "renew" in pricing.get("capacities", [])
        and pricing.get("interval") == 1
        and pricing.get("intervalUnit") == "month"
        and pricing.get("commitment") == 0
        and pricing.get("mode") == "default"
    ]
    if not candidates:
        return None
    price = candidates[0]
    return price.get("price", 0) / 100_000_000, price.get("formattedPrice", "")


def default_addon(plan: dict[str, Any], addons_by_code: dict[str, dict[str, Any]], family_name: str) -> str:
    for family in plan.get("addonFamilies", []):
        if family.get("name") == family_name:
            addon = addons_by_code.get(family.get("default"), {})
            invoice_name = addon.get("invoiceName")
            if isinstance(invoice_name, str):
                return invoice_name
            default_code = family.get("default")
            return default_code if isinstance(default_code, str) else ""
    return ""


def availability_by_plan(datacenter: str) -> dict[str, Counter[str]]:
    availability: dict[str, Counter[str]] = defaultdict(Counter)
    path = f"/dedicated/server/datacenter/availabilities?datacenters={datacenter}"
    for row in get_json(path):
        plan_code = row.get("planCode")
        for dc in row.get("datacenters", []):
            if dc.get("datacenter") == datacenter:
                availability[plan_code][dc.get("availability", "unknown")] += 1
    return availability


def catalog_rows(
    *, catalog_name: str, ovh_subsidiary: str, availability: dict[str, Counter[str]], max_monthly_usd: float
) -> list[ServerRow]:
    catalog = get_json(f"/order/catalog/public/{catalog_name}?ovhSubsidiary={ovh_subsidiary}")
    products = {product["name"]: product for product in catalog.get("products", [])}
    addons = {addon["planCode"]: addon for addon in catalog.get("addons", [])}
    rows = []

    for plan in catalog.get("plans", []):
        plan_code = plan.get("planCode", "")
        pricing = monthly_pricing(plan)
        counts = availability.get(plan_code, Counter())
        if pricing is None or not counts:
            continue

        monthly_usd, formatted = pricing
        if monthly_usd >= max_monthly_usd:
            continue

        product = products.get(plan.get("product"), {})
        server = product.get("blobs", {}).get("technical", {}).get("server", {})
        cpu = server.get("cpu", {})
        cpu_desc = (f"{cpu.get('model', '')} {cpu.get('cores', '?')}c/{cpu.get('threads', '?')}t").strip()
        rows.append(
            ServerRow(
                price=formatted or f"${monthly_usd:.2f} USD",
                monthly_usd=monthly_usd,
                plan=plan_code,
                name=plan.get("invoiceName", ""),
                cpu=cpu_desc,
                memory=default_addon(plan, addons, "memory"),
                storage=default_addon(plan, addons, "storage"),
                availability=", ".join(f"{key}:{counts[key]}" for key in sorted(counts)),
                orderable_now=any(key.startswith("1H") or key == "72H" for key in counts),
            )
        )

    return rows


def format_rows(rows: list[ServerRow]) -> str:
    lines = [
        f"{row.price:>10}  {row.plan:<18} {row.name:<44} "
        f"{row.availability:<34} {row.cpu:<34} {row.memory} / {row.storage}"
        for row in rows
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datacenter", default="hil", help="OVH datacenter code")
    parser.add_argument("--ovh-subsidiary", default="US", help="OVH subsidiary")
    parser.add_argument(
        "--max-monthly-usd", type=float, default=100, help="Exclude plans at or above this monthly price"
    )
    parser.add_argument(
        "--catalog",
        action="append",
        choices=DEFAULT_CATALOGS,
        help="Catalog to query; repeatable. Defaults to eco and baremetalServers.",
    )
    args = parser.parse_args()

    availability = availability_by_plan(args.datacenter)
    rows = []
    for catalog_name in args.catalog or DEFAULT_CATALOGS:
        rows.extend(
            catalog_rows(
                catalog_name=catalog_name,
                ovh_subsidiary=args.ovh_subsidiary,
                availability=availability,
                max_monthly_usd=args.max_monthly_usd,
            )
        )

    rows.sort(key=lambda row: (not row.orderable_now, row.monthly_usd, row.plan))
    if not rows:
        print(
            f"No plans found for datacenter={args.datacenter!r} below ${args.max_monthly_usd:.2f}/month",
            file=sys.stderr,
        )
        sys.exit(1)
    print(format_rows(rows), end="")


if __name__ == "__main__":
    main()
