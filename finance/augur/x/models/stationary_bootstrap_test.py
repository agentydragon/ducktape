"""Tests for the stationary block bootstrap over the replay's record.

The synthetic record lets every series name its own month — rates by level, the indices by the
growth into it — so a test reads back where each path month came from, series by series, without
trusting the sampler's account of it.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.bond_fund import BondFundSpec, YieldCurve
from finance.augur.model.equity import EquitySpec
from finance.augur.model.exogenous import ExogenousSamplingRequest, Sampler
from finance.augur.model.historical_windows import HistoricalWindowsModel, MacroHistory
from finance.augur.model.market_paths import MarketPaths
from finance.augur.model.series import SecuritySymbol
from finance.augur.x.models.stationary_bootstrap import StationaryBootstrapModel

RATE_STEP = 1e-4
EQUITY_GROWTH = 0.004
CPI_GROWTH = 0.0015
GROWTH_STEP = 1e-6


def _record(months: int) -> MacroHistory:
    """Rates rise by a fixed step and the indices' log growth by another, so no two months share
    a value of any series."""

    index = np.arange(months, dtype=np.float64)
    return MacroHistory(
        months=tuple(date(1900 + month // 12, month % 12 + 1, 1) for month in range(months)),
        short_rate=0.01 + index * RATE_STEP,
        term_spread=0.005 + index * RATE_STEP,
        corporate_aaa_yield=0.02 + index * RATE_STEP,
        corporate_baa_yield=0.03 + index * RATE_STEP,
        equity_level=np.exp(np.cumsum(EQUITY_GROWTH + index * GROWTH_STEP)),
        cpi_level=np.exp(np.cumsum(CPI_GROWTH + index * GROWTH_STEP)),
    )


def _request(*, horizon_months: int, rollouts: int) -> ExogenousSamplingRequest:
    return ExogenousSamplingRequest(horizon_months=horizon_months, rollout_seeds=tuple(range(1000, 1000 + rollouts)))


def _read_back(paths: MarketPaths) -> np.ndarray:
    """The record month of every path month, read off each series separately; they must agree."""

    assert paths.equity_total_return_index is not None
    by_level = [
        (paths.short_rate - 0.01) / RATE_STEP,
        (paths.term_spread - 0.005) / RATE_STEP,
        (paths.corporate_yields[YieldCurve.CORPORATE_AAA] - 0.02) / RATE_STEP,
        (paths.corporate_yields[YieldCurve.CORPORATE_BAA] - 0.03) / RATE_STEP,
    ]
    # The opening has no growth into it, so only the rates name its month.
    by_growth = [
        (np.log(index[:, 1:] / index[:, :-1]) - base) / GROWTH_STEP
        for index, base in ((paths.equity_total_return_index, EQUITY_GROWTH), (paths.cpi_level, CPI_GROWTH))
    ]
    months = np.rint(by_level[0]).astype(np.int64)
    for read in by_level:
        np.testing.assert_allclose(read, months, atol=1e-6)
    for read in by_growth:
        np.testing.assert_allclose(read, months[:, 1:], atol=1e-6)
    return months


def _continues(months: np.ndarray, *, record_months: int) -> np.ndarray:
    """Whether each path month from the second on is the record month after its predecessor's,
    which past the last month is the second: the first with growth into it."""

    return np.asarray(months[:, 2:] == months[:, 1:-1] % (record_months - 1) + 1)


@pytest.mark.parametrize("mean_block_months", [1.0, 12.0])
def test_every_path_month_is_one_record_month_for_every_series(mean_block_months: float) -> None:
    """The cross-section survives: a month's rates, credit curves, equity and CPI growth all come
    from the same record month, which is the one `source_months` names."""

    model = StationaryBootstrapModel(history=_record(240), mean_block_months=mean_block_months)
    request = _request(horizon_months=120, rollouts=20)

    np.testing.assert_array_equal(_read_back(model.sample_market(request)), model.source_months(request))


def test_a_block_is_the_replay_window_it_starts_at() -> None:
    """Consecutive months, rebased as the replay rebases: before it wraps, a block IS a replay
    window in every observable, so the two samplers differ only in where blocks start and end."""

    record = _record(240)
    model = StationaryBootstrapModel(history=record, mean_block_months=1e9)
    request = _request(horizon_months=120, rollouts=6)
    sampled = model.sample_market(request)
    replay = HistoricalWindowsModel(history=record)

    for rollout, opening in enumerate(model.source_months(request)[:, 0]):
        horizon = min(120, len(record.months) - 1 - int(opening))
        window = replay.market_paths(window_starts=(record.months[opening],), horizon_months=horizon)
        for name, replayed, bootstrapped in _paired(window, sampled):
            np.testing.assert_allclose(bootstrapped[rollout, : horizon + 1], replayed[0], rtol=1e-12, err_msg=name)


def _paired(left: MarketPaths, right: MarketPaths) -> list[tuple[str, np.ndarray, np.ndarray]]:
    assert left.equity_total_return_index is not None
    assert right.equity_total_return_index is not None
    return [
        ("short_rate", left.short_rate, right.short_rate),
        ("term_spread", left.term_spread, right.term_spread),
        ("cpi_level", left.cpi_level, right.cpi_level),
        ("equity", left.equity_total_return_index, right.equity_total_return_index),
        *((str(curve), left.corporate_yields[curve], right.corporate_yields[curve]) for curve in left.corporate_yields),
    ]


def test_a_horizon_longer_than_the_record_wraps_around_it() -> None:
    """Over fourteen times the record's length: past its last month a block continues at the second."""

    record_months = 25
    model = StationaryBootstrapModel(
        history=_record(record_months),
        mean_block_months=1e9,
        equity=EquitySpec(symbol=SecuritySymbol("TEST_EQ"), initial_price_usd=50.0),
        instruments=(BondFundSpec(symbol=SecuritySymbol("TEST_BOND"), maturity_years=6.0),),
    )
    request = _request(horizon_months=360, rollouts=4)
    months = _read_back(model.sample_market(request))

    openings = months[:, :1]
    np.testing.assert_array_equal(months[:, 1:], (openings + np.arange(360)) % (record_months - 1) + 1)
    bundle = model.sample(request)
    for key in model.emittable_level_keys():
        assert np.all(np.isfinite(bundle.level_matrix(key, rollout_count=4, horizon_months=360)))


# The ends of the 12-240 month sensitivity range in Anarkulova, Cederburg, O'Doherty & Sias, "The Safe
# Withdrawal Rate: Evidence from a Broad Sample of Developed Markets" (JPEF 24(3), 2025).
@pytest.mark.parametrize("mean_block_months", [12.0, 240.0])
def test_blocks_last_the_configured_mean_on_average(mean_block_months: float) -> None:
    """Measured as the rate at which months start a block rather than as the average finished
    run, which the horizon truncates. A fresh block that happens to start at the successor reads
    as a continuation, one time in `record_months - 1`: far inside the tolerance here."""

    record_months = 2401
    model = StationaryBootstrapModel(history=_record(record_months), mean_block_months=mean_block_months)
    starts = ~_continues(model.source_months(_request(horizon_months=480, rollouts=1000)), record_months=record_months)

    start_rate = 1.0 / mean_block_months
    relative_standard_error = math.sqrt((1.0 - start_rate) / (start_rate * starts.size))
    assert 1.0 / starts.mean() == pytest.approx(mean_block_months, rel=4 * relative_standard_error)


def test_a_mean_block_of_one_month_is_the_iid_monthly_bootstrap() -> None:
    """Every month starts a block, so the next month is the record successor no more often than
    any other month is drawn: once in `record_months - 1`, not in most months as blocks would."""

    record_months = 12
    model = StationaryBootstrapModel(history=_record(record_months), mean_block_months=1.0)
    continues = _continues(model.source_months(_request(horizon_months=100, rollouts=200)), record_months=record_months)

    iid_rate = 1.0 / (record_months - 1)
    assert abs(continues.mean() - iid_rate) < 4 * math.sqrt(iid_rate * (1.0 - iid_rate) / continues.size)


def test_a_rollout_depends_only_on_its_own_seed() -> None:
    """Split the seeds across calls, reverse them, or ask for a shorter horizon: each rollout's
    path is the same, so sharded runs concatenate to the batch run bit for bit."""

    model = StationaryBootstrapModel(history=_record(120), mean_block_months=6.0)
    seeds = tuple(range(40, 50))
    whole = model.sample_market(ExogenousSamplingRequest(horizon_months=240, rollout_seeds=seeds))
    shards = [
        model.sample_market(ExogenousSamplingRequest(horizon_months=240, rollout_seeds=shard))
        for shard in (seeds[:3], seeds[3:])
    ]
    backwards = model.sample_market(ExogenousSamplingRequest(horizon_months=240, rollout_seeds=seeds[::-1]))
    shorter = model.sample_market(ExogenousSamplingRequest(horizon_months=60, rollout_seeds=seeds))

    for (name, batch, first_shard), (_, _, second_shard) in zip(
        _paired(whole, shards[0]), _paired(whole, shards[1]), strict=True
    ):
        np.testing.assert_array_equal(np.concatenate([first_shard, second_shard]), batch, err_msg=name)
    for name, batch, reordered in _paired(whole, backwards):
        np.testing.assert_array_equal(reordered[::-1], batch, err_msg=name)
    for name, batch, prefix in _paired(whole, shorter):
        np.testing.assert_array_equal(prefix, batch[:, :61], err_msg=name)


def test_a_longer_mean_only_drops_block_starts() -> None:
    """Common random numbers across a block-length sweep, here the ends of the 12-240 month range
    swept by Anarkulova, Cederburg, O'Doherty & Sias (JPEF 2025): wherever the longer mean starts a
    block, the shorter one starts one too, at the same record month."""

    record = _record(600)
    request = _request(horizon_months=240, rollouts=50)
    shorter = StationaryBootstrapModel(history=record, mean_block_months=12.0).source_months(request)
    longer = StationaryBootstrapModel(history=record, mean_block_months=240.0).source_months(request)
    longer_starts = ~_continues(longer, record_months=600)

    assert longer_starts.any()
    np.testing.assert_array_equal(shorter[:, 2:][longer_starts], longer[:, 2:][longer_starts])
    np.testing.assert_array_equal(shorter[:, :2], longer[:, :2])


@pytest.mark.parametrize("mean_block_months", [0.0, -12.0, 0.5, math.inf, math.nan])
def test_a_mean_block_length_under_one_month_or_not_finite_is_rejected(mean_block_months: float) -> None:
    """A geometric length on 1, 2, ... months cannot average less than one."""

    with pytest.raises(ValueError, match="mean_block_months"):
        StationaryBootstrapModel(history=_record(24), mean_block_months=mean_block_months)


@pytest.mark.parametrize("record_months", [0, 1])
def test_a_record_without_a_month_to_month_growth_is_rejected(record_months: int) -> None:
    with pytest.raises(ValueError, match="at least two"):
        StationaryBootstrapModel(history=_record(record_months), mean_block_months=12.0)


def test_the_provenance_names_the_record_the_mean_and_the_seeds() -> None:
    """Enough to reproduce every path — and the caveat that none of them is new history."""

    record = _record(120)
    model = StationaryBootstrapModel(history=record, mean_block_months=36.0)
    provenance = model.sample_market(ExogenousSamplingRequest(horizon_months=12, rollout_seeds=(7, 8))).provenance

    assert (provenance["record_start"], provenance["record_end"]) == ("1900-01-01", "1909-12-01")
    assert provenance["mean_block_months"] == 36.0
    assert provenance["rollout_seeds"] == (7, 8)
    assert "bounded by the record" in str(provenance["notes"])


def test_a_sample_emits_exactly_the_series_it_advertises() -> None:
    """What sample-sanity partitions against. The bootstrap carries observed corporate yields, so
    a credit sleeve prices off them."""

    model: Sampler = StationaryBootstrapModel(
        history=_record(120),
        mean_block_months=12.0,
        equity=EquitySpec(symbol=SecuritySymbol("TEST_EQ"), initial_price_usd=50.0),
        instruments=(
            BondFundSpec(
                symbol=SecuritySymbol("TEST_CREDIT"), maturity_years=8.0, yield_curve=YieldCurve.CORPORATE_BAA
            ),
            BondFundSpec(symbol=SecuritySymbol("TEST_CASH"), maturity_years=0.0, initial_price_usd=1.0),
        ),
    )

    bundle = model.sample(_request(horizon_months=24, rollouts=3))

    assert bundle.levels.series_keys() == model.emittable_level_keys()
    assert model.emittable_private_equity_issuers() == frozenset()


if __name__ == "__main__":
    pytest_bazel.main()
