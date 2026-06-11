"""augur-evidence CronJob entrypoint: the generic scraper + augur catalog rosters.

Extends the generic CLI (`fetch.build_parser`) with `--catalog`: every market a
calibration catalog references is mirrored (snapshot depth per platform defaults), so
the calibration server's checkout-based price reads always have data for the catalog
it scores. This is the only scraper module that imports `finance.augur` — the catalog
schema is calibration-specific; everything reusable lives in `finance.evidence` and
the sibling scraper modules.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from itertools import chain
from pathlib import Path

from finance.augur.calibration.catalog import MarketCatalog
from finance.evidence.markets import MarketEntry, load_roster, merged_roster
from finance.scraper import fetch


def build_parser() -> argparse.ArgumentParser:
    parser = fetch.build_parser()
    parser.add_argument(
        "--catalog",
        action="append",
        type=Path,
        default=[],
        help="Calibration catalog YAML whose referenced markets join the mirror roster; repeatable.",
    )
    return parser


def markets_from_args(args: argparse.Namespace) -> tuple[MarketEntry, ...]:
    return merged_roster(
        chain.from_iterable(load_roster(path) for path in args.roster),
        chain.from_iterable(sorted(MarketCatalog.from_yaml(path).referenced_markets()) for path in args.catalog),
    )


async def async_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return await fetch.run_from_args(args, markets=markets_from_args(args))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    sys.exit(main())
