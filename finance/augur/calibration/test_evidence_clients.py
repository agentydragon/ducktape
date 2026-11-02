"""Parse fidelity of the mirror-backed readers over fixture snapshots.

Fixture shapes mirror what the scraper stores: the verbatim platform response bodies
(verified against the live APIs 2026-06-10), including Kalshi's `{"market": ...}`
wrapper with stringified decimals and gamma's raw one-element list form.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_bazel

from finance.augur.calibration.evidence_clients import EvidenceMarketReader, MarketNotMirroredError
from finance.augur.calibration.quote import BookQuote, PoolQuote
from finance.evidence.manifest import EVIDENCE_META_FILENAME, EvidenceManifest
from finance.evidence.markets import Platform, market_json_path


def _write_snapshot(evidence_dir: Path, platform: Platform, market_id: str, payload: object) -> None:
    path = market_json_path(evidence_dir, platform, market_id)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload))


def _write_manifest(evidence_dir: Path, last_fetched: dict[str, datetime]) -> None:
    (evidence_dir / EVIDENCE_META_FILENAME).write_text(EvidenceManifest(last_fetched=last_fetched).model_dump_json())


async def test_manifold_binary_market(tmp_path: Path) -> None:
    fetched_at = datetime(2026, 10, 31, 12, 30, tzinfo=UTC)
    _write_snapshot(
        tmp_path,
        Platform.MANIFOLD,
        "m1",
        {
            "id": "m1",
            "url": "https://manifold.markets/x/btc-50k",
            "question": "Will BTC hit $50k?",
            "probability": 0.139,
            "volume": 310919.12,
            "textDescription": "Resolves YES if BTC trades at $50k.",
            "outcomeType": "BINARY",
        },
    )
    _write_manifest(tmp_path, {"manifold:m1": fetched_at})
    market = await EvidenceMarketReader(platform=Platform.MANIFOLD, evidence_dir=tmp_path).get_market("m1")
    assert market.quote == PoolQuote(price=0.139)
    assert market.require_implied_probability() == 0.139
    assert market.url == "https://manifold.markets/x/btc-50k"
    assert market.title == "Will BTC hit $50k?"
    assert market.rules == "Resolves YES if BTC trades at $50k."
    assert market.volume == 310919.12
    assert market.volume_unit == "𝕄"  # noqa: RUF001
    assert market.last_successful_fetch_at == fetched_at


@pytest.mark.parametrize(
    "manifest_text",
    ["not json", '{"last_fetched":{"manifold:m1":"2026-10-31T12:30:00"}}'],
    ids=["malformed-json", "naive-timestamp"],
)
async def test_invalid_manifest_metadata_stays_unknown(tmp_path: Path, manifest_text: str) -> None:
    _write_snapshot(
        tmp_path, Platform.MANIFOLD, "m1", {"id": "m1", "url": "https://manifold.markets/x/m1", "probability": 0.5}
    )
    (tmp_path / EVIDENCE_META_FILENAME).write_text(manifest_text)

    market = await EvidenceMarketReader(platform=Platform.MANIFOLD, evidence_dir=tmp_path).get_market("m1")

    assert market.last_successful_fetch_at is None


async def test_manifold_multiple_choice_has_no_quote(tmp_path: Path) -> None:
    _write_snapshot(
        tmp_path,
        Platform.MANIFOLD,
        "mc",
        {"id": "mc", "url": "https://manifold.markets/x/mc", "outcomeType": "MULTIPLE_CHOICE"},
    )
    market = await EvidenceMarketReader(platform=Platform.MANIFOLD, evidence_dir=tmp_path).get_market("mc")
    assert market.quote is None
    assert market.last_successful_fetch_at is None


async def test_kalshi_market_maps_placeholder_sides_to_none(tmp_path: Path) -> None:
    _write_snapshot(
        tmp_path,
        Platform.KALSHI,
        "KXT-1",
        {
            "market": {
                "ticker": "KXT-1",
                "title": "CPI YoY",
                "yes_sub_title": "Above 3.0%",
                "rules_primary": "Resolves YES if CPI YoY is above 3.0%.",
                # A 0 bid is Kalshi's untraded placeholder, not a real quote.
                "yes_bid_dollars": "0.0000",
                "yes_ask_dollars": "0.1500",
                "yes_bid_size_fp": "0.00",
                "yes_ask_size_fp": "800.00",
                "last_price_dollars": "0.1300",
                "volume_fp": "10250.00",
            }
        },
    )
    market = await EvidenceMarketReader(platform=Platform.KALSHI, evidence_dir=tmp_path).get_market("KXT-1")
    assert market.quote == BookQuote(bid=None, ask=0.15, bid_size=None, ask_size=800.0, last_trade=0.13)
    assert market.title is not None
    assert "CPI YoY" in market.title
    assert "Above 3.0%" in market.title  # the leg clause (yes_sub_title) is folded into the question
    assert market.rules == "Resolves YES if CPI YoY is above 3.0%."
    assert market.volume == 10250.0
    assert market.volume_unit == "contracts"
    assert market.url == "https://kalshi.com/markets/KXT-1"


async def test_polymarket_condition_id_list_snapshot(tmp_path: Path) -> None:
    # Condition-id snapshots are stored as the raw one-element gamma list.
    _write_snapshot(
        tmp_path,
        Platform.POLYMARKET,
        "0xdd224",
        [
            {
                "id": "253591",
                "question": "Will Trump win?",
                "conditionId": "0xdd224",
                "slug": "will-trump-win",
                "description": "Resolves YES if Trump wins.",
                "bestBid": 0.997,
                "bestAsk": 0.998,
                "lastTradePrice": 1,
                "volumeNum": 1531479284.5,
            }
        ],
    )
    market = await EvidenceMarketReader(platform=Platform.POLYMARKET, evidence_dir=tmp_path).get_market("0xdd224")
    # Gamma carries no resting size: a depthless book whose micro-price degenerates to the mid.
    assert market.quote == BookQuote(bid=0.997, ask=0.998, bid_size=None, ask_size=None, last_trade=1.0)
    assert market.require_implied_probability() == pytest.approx(0.9975)
    assert market.url == "https://polymarket.com/event/will-trump-win"
    assert market.volume_unit == "USD"


async def test_missing_snapshot_raises_market_not_mirrored(tmp_path: Path) -> None:
    reader = EvidenceMarketReader(platform=Platform.MANIFOLD, evidence_dir=tmp_path)
    with pytest.raises(MarketNotMirroredError, match="never-scraped"):
        await reader.get_market("never-scraped")


async def test_corrupt_snapshot_propagates(tmp_path: Path) -> None:
    # A snapshot that exists but doesn't parse is corrupt mirror data — a bug, not a
    # droppable row.
    path = market_json_path(tmp_path, Platform.MANIFOLD, "bad")
    path.parent.mkdir(parents=True)
    path.write_text("not json")
    reader = EvidenceMarketReader(platform=Platform.MANIFOLD, evidence_dir=tmp_path)
    with pytest.raises(ValueError, match="Invalid JSON"):
        await reader.get_market("bad")


if __name__ == "__main__":
    pytest_bazel.main()
