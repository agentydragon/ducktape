"""Tests for the eager market-cache warmer (`warm_cache`)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
import pytest_bazel

from finance.augur.calibration.catalog import CorrelateMarket, ExactMarket, IpoByDateMapping, ManifoldRef, MarketCatalog
from finance.augur.calibration.platform import Market
from finance.augur.calibration.quote import BookQuote
from finance.augur.calibration.warm_cache import WarmSummary, _run, warm_market_cache
from finance.evidence.markets import Platform


class _RecordingClient:
    """A `PriceClient` recording every fetched id; chosen ids raise an httpx error to model a flap."""

    def __init__(self, *, fail_ids: frozenset[str] = frozenset()) -> None:
        self.calls: list[str] = []
        self._fail_ids = fail_ids

    async def get_market(self, market_id: str) -> Market:
        self.calls.append(market_id)
        if market_id in self._fail_ids:
            raise httpx.ConnectError("boom")
        return Market(
            id=market_id,
            url=f"https://test.example/{market_id}",
            quote=BookQuote(bid=0.5, ask=0.5, bid_size=None, ask_size=None, last_trade=0.5),
        )

    async def aclose(self) -> None:
        pass


def _catalog() -> MarketCatalog:
    """Two distinct Manifold markets, where one id ("AAA") backs both an exact and a correlate row."""
    return MarketCatalog(
        metadata={"as_of": "2026-05-29"},
        markets=[
            ExactMarket(
                platform_ref=ManifoldRef(manifold_id="AAA"),
                mapping=IpoByDateMapping(issuer="openai", by_date=date(2027, 1, 1)),
            ),
            CorrelateMarket(platform_ref=ManifoldRef(manifold_id="AAA"), correlate_of="ipo_by_date"),
            CorrelateMarket(platform_ref=ManifoldRef(manifold_id="BBB"), correlate_of="ipo_by_date"),
        ],
    )


async def test_warms_each_referenced_market_once() -> None:
    """A market backing several catalog rows is fetched once; every fetch populates the cache."""
    client = _RecordingClient()
    summary = await warm_market_cache(_catalog(), {Platform.MANIFOLD: client})
    assert sorted(client.calls) == ["AAA", "BBB"]
    assert summary == WarmSummary(requested=2, succeeded=2, failed=0)


async def test_failing_market_is_counted_not_raised() -> None:
    """A flapping upstream skips just that market — the pass continues and reports it failed."""
    client = _RecordingClient(fail_ids=frozenset({"BBB"}))
    summary = await warm_market_cache(_catalog(), {Platform.MANIFOLD: client})
    assert summary == WarmSummary(requested=2, succeeded=1, failed=1)
    assert sorted(client.calls) == ["AAA", "BBB"]


async def test_run_requires_cache_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The CLI refuses to warm a process-local cache (no AUGUR_MARKET_CACHE_URL): those snapshots
    would die with the pod and serve nobody. The guard fires before any catalog read or network."""
    monkeypatch.delenv("AUGUR_MARKET_CACHE_URL", raising=False)
    monkeypatch.delenv("AUGUR_CACHE_URL", raising=False)
    with pytest.raises(RuntimeError, match="AUGUR_MARKET_CACHE_URL"):
        await _run(["--catalog", str(tmp_path / "missing.yaml")])


if __name__ == "__main__":
    pytest_bazel.main()
