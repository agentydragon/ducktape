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
    gross: str


class LocationPrice(BaseModel, frozen=True):
    location: str
    price_monthly: PriceAmount


class Deprecation(BaseModel, frozen=True):
    unavailable_after: datetime


class LocationAvailability(BaseModel, frozen=True):
    name: str
    deprecation: Deprecation | None = None


class HCloudServerType(BaseModel, frozen=True):
    name: str
    cores: int
    cpu_type: str
    memory: float
    disk: int
    architecture: str
    deprecated: bool
    deprecation: Deprecation | None = None
    prices: list[LocationPrice]
    locations: list[LocationAvailability]

    def location_deprecation(self, location: str) -> Deprecation | None:
        loc = first((loc for loc in self.locations if loc.name == location), default=None)
        return loc.deprecation if loc else None


def fetch_server_types(location: str) -> list[tuple[HCloudServerType, LocationPrice]]:
    result = subprocess.run(["hcloud", "server-type", "list", "-o", "json"], capture_output=True, text=True, check=True)
    priced = []
    for raw in json.loads(result.stdout):
        server = HCloudServerType.model_validate(raw)
        price = first((p for p in server.prices if p.location == location), default=None)
        if price is not None:
            priced.append((server, price))
    return sorted(priced, key=lambda row: row[0].name)


def format_table(rows: list[tuple[HCloudServerType, LocationPrice]], location: str) -> str:
    header = (
        f"{'Name':<10} {'Cores':>5} {'CPU Type':<10} {'Arch':<6}"
        f" {'Memory':>9} {'Disk':>9} {'Monthly':>10}  {'Deprecation'}"
    )
    sep = "-" * len(header)
    lines = [header, sep]
    for r, price in rows:
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
        raise SystemExit(1)

    table = format_table(rows, args.location)
    if args.output:
        args.output.write_text(table)
        print(f"Wrote {len(rows)} server types to {args.output}")
    else:
        print(table, end="")


if __name__ == "__main__":
    main()
