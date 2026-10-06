from pathlib import Path

import pytest
import pytest_bazel

from finance.evidence.markets import (
    MarketEntry,
    Platform,
    bets_jsonl_path,
    comments_jsonl_path,
    load_roster,
    market_json_path,
    merged_roster,
)


def test_load_roster_rejects_unknown_keys(tmp_path: Path) -> None:
    roster = tmp_path / "roster.yaml"
    roster.write_text("markets:\n  - platform: manifold\n    market_id: x\n    depth: 3\n")
    with pytest.raises(ValueError, match="depth"):
        load_roster(roster)


@pytest.mark.parametrize("market_id", ["", "a/b", ".hidden"])
def test_entry_rejects_unsafe_ids(market_id: str) -> None:
    with pytest.raises(ValueError, match="invalid"):
        MarketEntry(platform=Platform.MANIFOLD, market_id=market_id)


def test_entry_rejects_deep_off_manifold() -> None:
    with pytest.raises(ValueError, match="Manifold-only"):
        MarketEntry(platform=Platform.KALSHI, market_id="KXTEST-26", deep=True)


def test_layout_paths(tmp_path: Path) -> None:
    assert market_json_path(tmp_path, Platform.MANIFOLD, "m1") == tmp_path / "markets/manifold/m1/market.json"
    assert bets_jsonl_path(tmp_path, Platform.MANIFOLD, "m1") == tmp_path / "markets/manifold/m1/bets.jsonl"
    assert comments_jsonl_path(tmp_path, Platform.MANIFOLD, "m1") == tmp_path / "markets/manifold/m1/comments.jsonl"


def test_merged_roster_dedupes_deep_wins() -> None:
    deep = MarketEntry(platform=Platform.MANIFOLD, market_id="m1", deep=True)
    shallow_dupe = MarketEntry(platform=Platform.MANIFOLD, market_id="m1")
    # Deep wins in either order.
    assert merged_roster([deep, shallow_dupe]) == (deep,)
    assert merged_roster([shallow_dupe, deep]) == (deep,)


def test_merged_roster_catalog_refs_use_default_depth() -> None:
    merged = merged_roster([], [(Platform.MANIFOLD, "m1"), (Platform.KALSHI, "KXTEST-26")])
    assert merged == (
        MarketEntry(platform=Platform.KALSHI, market_id="KXTEST-26", deep=False),
        MarketEntry(platform=Platform.MANIFOLD, market_id="m1", deep=True),
    )


def test_merged_roster_catalog_ref_deepens_existing_entry() -> None:
    shallow = MarketEntry(platform=Platform.MANIFOLD, market_id="m1")
    (merged,) = merged_roster([shallow], [(Platform.MANIFOLD, "m1")])
    assert merged.deep


if __name__ == "__main__":
    pytest_bazel.main()
