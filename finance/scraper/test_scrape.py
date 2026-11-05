import pytest_bazel
from python.runfiles import Runfiles

from finance.augur.calibration.catalog import MarketCatalog
from finance.evidence.markets import Platform, load_roster
from finance.scraper.scrape import build_parser, markets_from_args

_ROSTER = "ducktape/finance/evidence/example_market_roster.yaml"
_CATALOG = "ducktape/finance/augur/calibration/example_openai_catalog.yaml"


def test_markets_from_args_unions_roster_and_catalog(runfiles: Runfiles) -> None:
    root = runfiles.root()
    roster, catalog = root / _ROSTER, root / _CATALOG
    args = build_parser().parse_args(
        ["--git-url", "https://unused.test/repo.git", "--roster", str(roster), "--catalog", str(catalog)]
    )
    markets = markets_from_args(args)

    by_key = {(entry.platform, entry.market_id): entry for entry in markets}
    assert len(by_key) == len(markets)  # deduped
    for entry in load_roster(roster):
        assert by_key[(entry.platform, entry.market_id)].deep == entry.deep
    for platform, market_id in MarketCatalog.from_yaml(catalog).referenced_markets():
        # Catalog refs join with the platform's default depth: manifold deep, others shallow.
        assert by_key[(platform, market_id)].deep == (platform is Platform.MANIFOLD)


def test_catalog_flag_is_optional(runfiles: Runfiles) -> None:
    roster = runfiles.root() / _ROSTER
    args = build_parser().parse_args(["--git-url", "https://unused.test/repo.git", "--roster", str(roster)])
    assert markets_from_args(args) == tuple(sorted(load_roster(roster), key=lambda e: (e.platform, e.market_id)))


if __name__ == "__main__":
    pytest_bazel.main()
