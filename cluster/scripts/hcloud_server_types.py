"""List Hetzner Cloud server types for price comparisons."""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from more_itertools import first
from pydantic import BaseModel


class PriceAmount(BaseModel, frozen=True):
    net: str
    gross: str


class LocationPrice(BaseModel, frozen=True):
    location: str
    price_hourly: PriceAmount
    price_monthly: PriceAmount
    included_traffic: int
    price_per_tb_traffic: PriceAmount


class Deprecation(BaseModel, frozen=True):
    announced: datetime
    unavailable_after: datetime


class LocationAvailability(BaseModel, frozen=True):
    id: int
    name: str
    deprecation: Deprecation | None = None


class HCloudServerType(BaseModel, frozen=True):
    name: str
    description: str
    cores: int
    cpu_type: str
    memory: float
    disk: int
    architecture: str
    storage_type: str
    deprecated: bool
    deprecation: Deprecation | None = None
    prices: list[LocationPrice]
    locations: list[LocationAvailability]

    def price_for(self, location: str) -> LocationPrice | None:
        return first((p for p in self.prices if p.location == location), default=None)

    def location_deprecation(self, location: str) -> Deprecation | None:
        loc = first((loc for loc in self.locations if loc.name == location), default=None)
        return loc.deprecation if loc else None


def fetch_server_types(location: str) -> list[HCloudServerType]:
    result = subprocess.run(["hcloud", "server-type", "list", "-o", "json"], capture_output=True, text=True, check=True)
    all_types = [HCloudServerType.model_validate(st) for st in json.loads(result.stdout)]
    return sorted([st for st in all_types if st.price_for(location)], key=lambda s: s.name)


def format_table(rows: list[HCloudServerType], location: str) -> str:
    header = (
        f"{'Name':<10} {'Cores':>5} {'CPU Type':<10} {'Arch':<6}"
        f" {'Memory':>9} {'Disk':>9} {'Monthly':>10}  {'Deprecation'}"
    )
    sep = "-" * len(header)
    lines = [header, sep]
    for r in rows:
        price = r.price_for(location)
        assert price  # filtered in fetch_server_types
        dep = r.deprecation or r.location_deprecation(location)
        dep_str = f"unavail after {dep.unavailable_after:%Y-%m-%d}" if dep else ""
        if r.deprecated:
            dep_str = "DEPRECATED"
        lines.append(
            f"{r.name:<10} {r.cores:>5} {r.cpu_type:<10} {r.architecture:<6}"
            f" {r.memory:>7.1f}GB {r.disk:>7}GB"
            f" ${float(price.price_monthly.gross):>8.2f}  {dep_str}"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--location", default="hil", help="Hetzner location (default: hil)")
    parser.add_argument("-o", "--output", type=Path, help="Output file (default: stdout)")
    args = parser.parse_args()

    rows = fetch_server_types(args.location)
    if not rows:
        print(f"No server types found in location {args.location!r}", file=sys.stderr)
        sys.exit(1)

    table = format_table(rows, args.location)
    if args.output:
        args.output.write_text(table)
        print(f"Wrote {len(rows)} server types to {args.output}")
    else:
        print(table, end="")


if __name__ == "__main__":
    main()
