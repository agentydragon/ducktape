"""End-to-end `run_calibration` against a fixed-output fixture model (no network).

Uses the shared `ConstantFrameModel` fixture (augur.model.testing) seeded
with a per-rollout event array, and hermetic mock price clients so prices are
deterministic.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import numpy.typing as npt
import pytest
import pytest_bazel

from finance.augur.calibration.calibration import (
    _fit_ladder_curve,
    _monotone_probabilities,
    build_anchored_level_paths,
    mark_fan,
    run_calibration,
    sample_private_equity_bundle,
    wilson_interval,
)
from finance.augur.calibration.catalog import (
    BucketFamily,
    BucketMember,
    CorrelateMarket,
    DateLadderFamily,
    DateLadderMember,
    ExactMarket,
    InflationYoyMapping,
    IpoByDateMapping,
    KalshiRef,
    LevelAtDateMapping,
    LevelByDateMapping,
    ManifoldRef,
    MarketCatalog,
    PolymarketRef,
    PreIpoFailureMapping,
    ThresholdLadderFamily,
    ThresholdLadderMember,
)
from finance.augur.calibration.platform import Direction, Market, PriceClient
from finance.augur.calibration.quote import BookQuote, PoolQuote
from finance.augur.calibration.testing import KalshiRungQuote, mock_price_clients
from finance.augur.model.exogenous import ExogenousSamplingRequest
from finance.augur.model.private_equity_bundle import PrivateEquityFloatChannel
from finance.augur.model.series import SP500_SYMBOL, InflationKey, IssuerId, PrivateEquityEventKindCode, SecurityKey
from finance.augur.model.testing import ConstantFrameModel, PrivateEquityChannels
from finance.evidence.markets import Platform

_ISSUER = "issuer_x"
_HORIZON = 120


def _event_kind_codes(request: ExogenousSamplingRequest) -> npt.NDArray[np.int64]:
    """4 rollouts: IPO@7, IPO@60, collapse@3, stays-private (NONE everywhere)."""
    none = int(PrivateEquityEventKindCode.NONE)
    events = np.full((request.rollout_count, request.horizon_months + 1), none, dtype=np.int64)
    events[0, 7] = int(PrivateEquityEventKindCode.PUBLIC_MARKET_OPEN)
    events[1, 60] = int(PrivateEquityEventKindCode.PUBLIC_MARKET_OPEN)
    events[2, 3] = int(PrivateEquityEventKindCode.COLLAPSE)
    return events


@pytest.fixture
def model() -> ConstantFrameModel:
    return ConstantFrameModel(
        private_equity={
            IssuerId(_ISSUER): PrivateEquityChannels(mark_usd_per_unit=50.0, event_kind_code=_event_kind_codes)
        }
    )


@pytest.fixture
def catalog() -> MarketCatalog:
    """One exact ipo_by_date, one exact pre_ipo_failure, one correlate (ipo_by_date) market."""
    return MarketCatalog(
        metadata={"as_of": "2026-05-29", "augur_model_as_of": "2026-05-27"},
        markets=[
            ExactMarket(
                platform_ref=ManifoldRef(manifold_id="AAA"),
                resolution_deadline=date(2027, 1, 1),
                mapping=IpoByDateMapping(issuer=_ISSUER, by_date=date(2027, 1, 1)),
            ),
            ExactMarket(platform_ref=ManifoldRef(manifold_id="BBB"), mapping=PreIpoFailureMapping(issuer=_ISSUER)),
            CorrelateMarket(
                platform_ref=ManifoldRef(manifold_id="CCC"),
                resolution_deadline=date(2026, 12, 31),
                correlate_of="ipo_by_date",
                issuer=_ISSUER,
                correlate_strength="strong",
                reason="The >=$1T cap conjunct needs a valuation augur does not model.",
            ),
        ],
    )


@pytest.fixture
def price_clients() -> dict[Platform, PriceClient]:
    return mock_price_clients({Platform.MANIFOLD: {"AAA": 0.40, "BBB": 0.10, "CCC": 0.66}})


async def _run(model: ConstantFrameModel, catalog: MarketCatalog, price_clients: dict[Platform, PriceClient]):
    seeds = tuple(range(4))
    bundle = sample_private_equity_bundle(model, issuer=_ISSUER, horizon_months=_HORIZON, rollout_seeds=seeds)
    return await run_calibration(
        catalog, horizon_months=_HORIZON, rollout_seeds=seeds, price_clients=price_clients, bundle=bundle
    )


async def test_unmirrored_market_drops_its_row_only(model: ConstantFrameModel, catalog: MarketCatalog) -> None:
    """A catalog market the mirror has no snapshot for (e.g. an entry newer than the last
    scraper run) drops just that row; the rest of the run scores normally."""
    clients = mock_price_clients({Platform.MANIFOLD: {"BBB": 0.10, "CCC": 0.66}})  # no "AAA"
    result = await _run(model, catalog, clients)
    assert sorted(row.market_id for row in result.clean) == ["BBB"]
    assert sorted(row.market_id for row in result.surfaced) == ["CCC"]


async def test_clean_rows_score_events(
    model: ConstantFrameModel, catalog: MarketCatalog, price_clients: dict[Platform, PriceClient]
) -> None:
    result = await _run(model, catalog, price_clients)
    assert result.rollout_count == 4
    clean = {row.market_id: row for row in result.clean}

    # ipo_by_date(2027-01-01 -> month 7): rollout 0 YES; rollouts 1,3 NO (no IPO by then,
    # whole horizon simulated); rollout 2 NO (collapsed, no IPO). All resolved.
    ipo = clean["AAA"]
    assert ipo.platform == "manifold"
    assert ipo.n_resolved == 4
    assert ipo.unresolved == 0
    assert ipo.p_model == 0.25
    assert ipo.p_market == 0.40  # injected stub price
    # D_KL(market ‖ model) in bits for p_market=0.40 vs p_model=0.25.
    assert ipo.kl_bits is not None
    assert math.isclose(ipo.kl_bits, 0.07807190511263762)

    # pre_ipo_failure: rollout 2 YES (collapse before IPO); rollouts 0,1 NO (IPO first);
    # rollout 3 UNRESOLVED (still private at horizon end).
    fail = clean["BBB"]
    assert fail.n_resolved == 3
    assert fail.unresolved == 1
    assert fail.p_model is not None
    assert math.isclose(fail.p_model, 1 / 3)


async def test_surfaced_row_carries_augur_context(
    model: ConstantFrameModel, catalog: MarketCatalog, price_clients: dict[Platform, PriceClient]
) -> None:
    result = await _run(model, catalog, price_clients)
    assert [row.market_id for row in result.surfaced] == ["CCC"]
    surfaced = result.surfaced[0]
    assert surfaced.platform == "manifold"
    assert surfaced.correlate_of == "ipo_by_date"
    assert surfaced.p_market == 0.66  # injected stub price
    assert surfaced.reason == "The >=$1T cap conjunct needs a valuation augur does not model."
    # deadline 2026-12-31 is month 7 from as_of 2026-05-27; rollout 0 IPOs at month 7 (<=7),
    # the others don't -> P(IPO by deadline) = 1/4. This is surfaced context, NOT a score.
    assert surfaced.augur_context is not None
    assert surfaced.augur_context.signal == "P(PUBLIC_MARKET_OPEN by deadline)"
    assert surfaced.augur_context.p_model == 0.25


async def test_run_calibration_shares_bundle_with_mark_fan(
    model: ConstantFrameModel, catalog: MarketCatalog, price_clients: dict[Platform, PriceClient]
) -> None:
    # One sampled bundle drives both the clean/surfaced scoring and the issuer mark_fan, so
    # both views come from a single rollout.
    seeds = tuple(range(4))
    bundle = sample_private_equity_bundle(model, issuer=_ISSUER, horizon_months=_HORIZON, rollout_seeds=seeds)
    result = await run_calibration(
        catalog, horizon_months=_HORIZON, rollout_seeds=seeds, price_clients=price_clients, bundle=bundle
    )
    assert {row.market_id for row in result.clean} == {"AAA", "BBB"}
    fan = mark_fan(bundle, issuer=_ISSUER, rollout_count=4, horizon_months=_HORIZON, percentiles=(50.0,))
    assert fan.months[0].values == {"50.0": 50.0}


def test_mark_fan_shape(model: ConstantFrameModel) -> None:
    request = ExogenousSamplingRequest(
        horizon_months=_HORIZON,
        rollout_seeds=tuple(range(4)),
        required_private_equity_issuers=frozenset({IssuerId(_ISSUER)}),
    )
    bundle = model.sample(request).private_equity
    fan = mark_fan(bundle, issuer=_ISSUER, rollout_count=4, horizon_months=_HORIZON, percentiles=(5.0, 50.0, 95.0))
    assert fan.channel == PrivateEquityFloatChannel.MARK_USD_PER_UNIT
    assert fan.percentiles == [5.0, 50.0, 95.0]
    assert len(fan.months) == _HORIZON + 1
    # Constant 50.0 mark -> every percentile band is 50.0.
    assert fan.months[0].values == {"5.0": 50.0, "50.0": 50.0, "95.0": 50.0}


_SP500_ANCHOR = 6000.0


def _sp500_levels(request: ExogenousSamplingRequest) -> npt.NDArray[np.float64]:
    """4 rollouts: month 0 = anchor (so anchoring is identity); month 7 = [7000,7600,8000,5000]."""
    matrix = np.full((request.rollout_count, request.horizon_months + 1), _SP500_ANCHOR, dtype=np.float64)
    matrix[:, 7] = [7000.0, 7600.0, 8000.0, 5000.0]
    return matrix


@pytest.fixture
def macro_model() -> ConstantFrameModel:
    """A model emitting both the issuer's PE bundle and an sp500 level series (no inflation)."""
    return ConstantFrameModel(
        levels={SecurityKey(symbol=SP500_SYMBOL): _sp500_levels},
        private_equity={
            IssuerId(_ISSUER): PrivateEquityChannels(mark_usd_per_unit=50.0, event_kind_code=_event_kind_codes)
        },
    )


async def test_macro_level_market_scored_over_full_rollouts(macro_model: ConstantFrameModel) -> None:
    """A point-in-time S&P threshold market scores against the anchored sp500 channel, and a
    market on an unmodeled series (inflation) surfaces as `unmodeled` rather than failing."""
    catalog = MarketCatalog(
        metadata={"as_of": "2026-05-27", "anchors": {"security:SPY": _SP500_ANCHOR}},
        markets=[
            ExactMarket(
                platform_ref=ManifoldRef(manifold_id="SPX"),
                resolution_deadline=date(2026, 12, 31),
                mapping=LevelAtDateMapping(
                    series="security:SPY", threshold=7500.0, direction=Direction.ABOVE, at_date=date(2026, 12, 31)
                ),
            ),
            ExactMarket(
                platform_ref=ManifoldRef(manifold_id="SPX-TOUCH"),
                resolution_deadline=date(2026, 12, 31),
                mapping=LevelByDateMapping(
                    series="security:SPY", threshold=7500.0, direction=Direction.ABOVE, by_date=date(2026, 12, 31)
                ),
            ),
            ExactMarket(
                platform_ref=ManifoldRef(manifold_id="CPI"),
                mapping=InflationYoyMapping(
                    series="inflation", threshold=0.03, direction=Direction.ABOVE, at_date=date(2026, 12, 31)
                ),
            ),
        ],
    )
    seeds = tuple(range(4))
    sampled = macro_model.sample(
        ExogenousSamplingRequest(
            horizon_months=_HORIZON,
            rollout_seeds=seeds,
            required_asset_prices=frozenset({SecurityKey(symbol=SP500_SYMBOL)}),
            required_private_equity_issuers=frozenset({IssuerId(_ISSUER)}),
        )
    )
    level_paths = build_anchored_level_paths(
        sampled,
        anchors={"security:SPY": _SP500_ANCHOR},
        requested_wire_ids=catalog.referenced_level_series(),
        rollout_count=4,
        horizon_months=_HORIZON,
    )
    clients = mock_price_clients({Platform.MANIFOLD: {"SPX": 0.30, "SPX-TOUCH": 0.45, "CPI": 0.20}})
    result = await run_calibration(
        catalog,
        horizon_months=_HORIZON,
        rollout_seeds=seeds,
        price_clients=clients,
        bundle=sampled.private_equity,
        level_paths=level_paths,
    )
    clean = {row.market_id: row for row in result.clean}
    spx = clean["SPX"]
    assert spx.channel == "security:SPY"
    # month 7 values [7000,7600,8000,5000] >= 7500 -> 2 of 4 YES.
    assert spx.p_model == 0.5
    assert spx.p_market == 0.30
    assert spx.kl_bits is not None
    # level_by_date (touch): the anchored path only deviates at month 7 here, so the same 2 of 4
    # rollouts touch 7500 by the deadline -> p_model 0.5 (dispatch + scoring wired end to end).
    assert clean["SPX-TOUCH"].channel == "security:SPY"
    assert clean["SPX-TOUCH"].p_model == 0.5
    # inflation isn't emitted by this preset -> surfaced as unmodeled, never 500.
    cpi = {row.market_id: row for row in result.surfaced}["CPI"]
    assert cpi.mappability == "unmodeled"
    assert cpi.p_market == 0.20


async def test_bucket_family_scored_as_multinomial(macro_model: ConstantFrameModel) -> None:
    catalog = MarketCatalog(
        metadata={"as_of": "2026-05-27", "anchors": {"security:SPY": _SP500_ANCHOR}},
        markets=[],
        bucket_families=[
            BucketFamily(
                family_id="spx_eoy",
                question="S&P 500 close on 2026-12-31",
                platform=Platform.KALSHI,
                series="security:SPY",
                at_date=date(2026, 12, 31),
                buckets=[
                    BucketMember(market_id="B-LO", label="<7000", high=7000.0),
                    BucketMember(market_id="B-MID", label="7000-8000", low=7000.0, high=8000.0),
                    BucketMember(market_id="B-HI", label=">=8000", low=8000.0),
                ],
            )
        ],
    )
    seeds = tuple(range(4))
    sampled = macro_model.sample(
        ExogenousSamplingRequest(
            horizon_months=_HORIZON,
            rollout_seeds=seeds,
            required_asset_prices=frozenset({SecurityKey(symbol=SP500_SYMBOL)}),
            required_private_equity_issuers=frozenset({IssuerId(_ISSUER)}),
        )
    )
    level_paths = build_anchored_level_paths(
        sampled,
        anchors={"security:SPY": _SP500_ANCHOR},
        requested_wire_ids=catalog.referenced_level_series(),
        rollout_count=4,
        horizon_months=_HORIZON,
    )
    # Live bucket prices 0.2/0.5/0.3 (already sum to 1); model month-7 counts [1,2,1] -> [0.25,0.5,0.25].
    clients = mock_price_clients({Platform.KALSHI: {"B-LO": 0.20, "B-MID": 0.50, "B-HI": 0.30}})
    result = await run_calibration(
        catalog,
        horizon_months=_HORIZON,
        rollout_seeds=seeds,
        price_clients=clients,
        bundle=sampled.private_equity,
        level_paths=level_paths,
    )
    assert len(result.categorical) == 1
    family = result.categorical[0]
    assert family.channel == "security:SPY"
    assert family.n_resolved == 4
    # Quarters are exact in float; market shares need rounding (0.2/0.3 aren't).
    assert [b.p_model for b in family.buckets] == [0.25, 0.5, 0.25]
    assert [round(b.p_market, 3) for b in family.buckets] == [0.2, 0.5, 0.3]
    # D_KL(market ‖ model) = 0.2 log2(0.2/0.25) + 0.5 log2(1) + 0.3 log2(0.3/0.25) ≈ 0.0146 bits.
    assert family.kl_bits is not None
    assert math.isclose(family.kl_bits, 0.0146, abs_tol=1e-3)


def _inflation_levels(request: ExogenousSamplingRequest) -> npt.NDArray[np.float64]:
    """4 rollouts: month 7 YoY rates = [2.0%, 3.25%, 3.75%, 5.0%] against history=100."""
    matrix = np.full((request.rollout_count, request.horizon_months + 1), 100.0, dtype=np.float64)
    matrix[:, 7] = [102.0, 103.25, 103.75, 105.0]
    return matrix


async def test_threshold_ladder_family_derives_categorical_distribution() -> None:
    model = ConstantFrameModel(
        levels={InflationKey(): _inflation_levels},
        private_equity={
            IssuerId(_ISSUER): PrivateEquityChannels(mark_usd_per_unit=50.0, event_kind_code=_event_kind_codes)
        },
    )
    catalog = MarketCatalog(
        metadata={"as_of": "2026-05-27", "anchors": {"inflation": 100.0}},
        markets=[],
        threshold_ladder_families=[
            ThresholdLadderFamily(
                family_id="cpi_yoy",
                question="CPI YoY threshold ladder",
                platform=Platform.KALSHI,
                series="inflation",
                value_kind="inflation_yoy",
                direction=Direction.ABOVE,
                at_date=date(2026, 12, 31),
                thresholds=[
                    ThresholdLadderMember(market_id="CPI-T3.0", threshold=0.03),
                    ThresholdLadderMember(market_id="CPI-T3.5", threshold=0.035),
                    ThresholdLadderMember(market_id="CPI-T4.0", threshold=0.04),
                ],
            )
        ],
    )
    seeds = tuple(range(4))
    sampled = model.sample(
        ExogenousSamplingRequest(
            horizon_months=_HORIZON,
            rollout_seeds=seeds,
            required_index_series=frozenset({InflationKey()}),
            required_private_equity_issuers=frozenset({IssuerId(_ISSUER)}),
        )
    )
    level_paths = build_anchored_level_paths(
        sampled,
        anchors={"inflation": 100.0},
        requested_wire_ids=catalog.referenced_level_series(),
        rollout_count=4,
        horizon_months=_HORIZON,
    )
    clients = mock_price_clients({Platform.KALSHI: {"CPI-T3.0": 0.90, "CPI-T3.5": 0.60, "CPI-T4.0": 0.20}})
    result = await run_calibration(
        catalog,
        horizon_months=_HORIZON,
        rollout_seeds=seeds,
        price_clients=clients,
        bundle=sampled.private_equity,
        level_paths=level_paths,
        inflation_history=[100.0] * 5,
    )

    assert result.clean == []
    assert len(result.categorical) == 1
    family = result.categorical[0]
    assert family.channel == "inflation"
    assert [bucket.label for bucket in family.buckets] == ["<= 3%", "3% to 3.5%", "3.5% to 4%", "> 4%"]
    assert [bucket.p_market for bucket in family.buckets] == pytest.approx([0.10, 0.30, 0.40, 0.20])
    assert [bucket.p_model for bucket in family.buckets] == [0.25, 0.25, 0.25, 0.25]
    assert family.kl_bits is not None


def test_weighted_isotonic_lets_confident_points_pin_the_curve() -> None:
    """A low-confidence rung that violates monotonicity is pooled toward its confident neighbor,
    rather than dragging the confident value to the unweighted midpoint."""
    values = [0.80, 0.60, 0.65, 0.20]  # the 0.60 -> 0.65 step violates a decreasing survival curve
    uniform = _monotone_probabilities(values, [1.0, 1.0, 1.0, 1.0], increasing=False)
    # T3.5 confident (weight 10), the violating T4.0 wide/low-confidence (weight 0.1).
    weighted = _monotone_probabilities(values, [1.0, 10.0, 0.1, 1.0], increasing=False)
    assert uniform[1] == pytest.approx(0.625)  # unweighted pools 0.60 & 0.65 to their mean
    assert weighted[1] < 0.605  # weighted keeps the confident rung near its own 0.60


def test_fit_ladder_curve_interpolates_unpriced_rungs() -> None:
    def market(mid: float) -> Market:
        return Market(
            id="x", url="u", quote=BookQuote(bid=mid, ask=mid, bid_size=None, ask_size=None, last_trade=mid), volume=1.0
        )

    # Position 1 is unpriced: the fitted survival curve interpolates between its priced neighbors
    # (0.8 at 0 and 0.4 at 2) to 0.6 — never injecting a 0.
    curve = _fit_ladder_curve([0.0, 1.0, 2.0, 3.0], [market(0.8), None, market(0.4), market(0.2)], increasing=False)
    assert curve == pytest.approx([0.8, 0.6, 0.4, 0.2])
    # Fewer than two priced rungs -> no curve.
    assert _fit_ladder_curve([0.0, 1.0], [market(0.5), None], increasing=False) is None


async def test_threshold_ladder_uses_quote_mids_and_interpolates_unpriced() -> None:
    """End-to-end on Kalshi mock responses: bucket masses come from order-book MIDS (not the spiky
    last trades) and an untraded rung is interpolated across, not dropped or read as zero."""
    model = ConstantFrameModel(
        levels={InflationKey(): _inflation_levels},
        private_equity={
            IssuerId(_ISSUER): PrivateEquityChannels(mark_usd_per_unit=50.0, event_kind_code=_event_kind_codes)
        },
    )
    catalog = MarketCatalog(
        metadata={"as_of": "2026-05-27", "anchors": {"inflation": 100.0}},
        markets=[],
        threshold_ladder_families=[
            ThresholdLadderFamily(
                family_id="cpi_yoy",
                question="CPI YoY threshold ladder",
                platform=Platform.KALSHI,
                series="inflation",
                value_kind="inflation_yoy",
                direction=Direction.ABOVE,
                at_date=date(2026, 12, 31),
                thresholds=[
                    ThresholdLadderMember(market_id="CPI-T3.0", threshold=0.03),
                    ThresholdLadderMember(market_id="CPI-T3.5", threshold=0.035),
                    ThresholdLadderMember(market_id="CPI-T4.0", threshold=0.04),
                ],
            )
        ],
    )
    seeds = tuple(range(4))
    sampled = model.sample(
        ExogenousSamplingRequest(
            horizon_months=_HORIZON,
            rollout_seeds=seeds,
            required_index_series=frozenset({InflationKey()}),
            required_private_equity_issuers=frozenset({IssuerId(_ISSUER)}),
        )
    )
    level_paths = build_anchored_level_paths(
        sampled,
        anchors={"inflation": 100.0},
        requested_wire_ids=catalog.referenced_level_series(),
        rollout_count=4,
        horizon_months=_HORIZON,
    )
    clients = mock_price_clients(
        {
            Platform.KALSHI: {
                # Tight books at mid 0.90 / 0.20; the last trades (0.10, 0.95) are deliberately
                # spiky and must be ignored in favor of the mids.
                "CPI-T3.0": KalshiRungQuote(
                    bid=0.88, ask=0.92, bid_size=100.0, ask_size=100.0, last=0.10, volume=500.0
                ),
                # Untraded one-sided rung (only a 1c-equivalent ask, no bid/last/volume) -> dropped
                # from the fit and interpolated across.
                "CPI-T3.5": KalshiRungQuote(ask=0.99, ask_size=75.0),
                "CPI-T4.0": KalshiRungQuote(
                    bid=0.18, ask=0.22, bid_size=100.0, ask_size=100.0, last=0.95, volume=500.0
                ),
            }
        }
    )
    result = await run_calibration(
        catalog,
        horizon_months=_HORIZON,
        rollout_seeds=seeds,
        price_clients=clients,
        bundle=sampled.private_equity,
        level_paths=level_paths,
        inflation_history=[100.0] * 5,
    )

    family = result.categorical[0]
    # Survival from mids: 0.90 @3.0, interpolated 0.55 @3.5, 0.20 @4.0 -> buckets below.
    assert [bucket.p_market for bucket in family.buckets] == pytest.approx([0.10, 0.35, 0.35, 0.20])


async def test_date_ladder_family_derives_event_timing_distribution(model: ConstantFrameModel) -> None:
    catalog = MarketCatalog(
        metadata={"as_of": "2026-05-27"},
        markets=[],
        date_ladder_families=[
            DateLadderFamily(
                family_id="openai_ipo_kalshi",
                question="OpenAI IPO timing ladder",
                platform=Platform.KALSHI,
                issuer=_ISSUER,
                dates=[
                    DateLadderMember(market_id="IPO-2026", by_date=date(2026, 12, 31)),
                    DateLadderMember(market_id="IPO-2031", by_date=date(2031, 6, 30)),
                ],
            )
        ],
    )
    seeds = tuple(range(4))
    bundle = sample_private_equity_bundle(model, issuer=_ISSUER, horizon_months=_HORIZON, rollout_seeds=seeds)
    clients = mock_price_clients({Platform.KALSHI: {"IPO-2026": 0.30, "IPO-2031": 0.70}})
    result = await run_calibration(
        catalog, horizon_months=_HORIZON, rollout_seeds=seeds, price_clients=clients, bundle=bundle
    )

    assert result.clean == []
    assert len(result.categorical) == 1
    family = result.categorical[0]
    assert family.channel == _ISSUER
    assert [bucket.label for bucket in family.buckets] == [
        "By 2026-12-31",
        "2026-12-31 to 2031-06-30",
        "After 2031-06-30",
    ]
    assert [bucket.p_market for bucket in family.buckets] == pytest.approx([0.30, 0.40, 0.30])
    # Fixture rollouts: IPO@7 -> first bucket, IPO@60 -> second, collapse + no-IPO -> final.
    assert [bucket.p_model for bucket in family.buckets] == [0.25, 0.25, 0.5]
    assert family.n_resolved == 4
    assert family.kl_bits is not None


class _ProbClient:
    """A minimal PriceClient returning a Market with a fixed (possibly None) implied probability."""

    def __init__(self, probabilities: dict[str, float | None]) -> None:
        self._probabilities = probabilities

    async def get_market(self, market_id: str) -> Market:
        probability = self._probabilities[market_id]
        return Market(
            id=market_id,
            url=f"https://test.example/{market_id}",
            quote=None if probability is None else PoolQuote(price=probability),
        )

    async def aclose(self) -> None:
        pass


async def test_none_probability_and_degenerate_family_are_dropped(macro_model: ConstantFrameModel) -> None:
    """A market whose quote carries no probability, and a categorical family whose bucket prices
    sum to zero, are both dropped (logged) rather than 500-ing via require_implied_probability()."""
    catalog = MarketCatalog(
        metadata={"as_of": "2026-05-27", "anchors": {"security:SPY": _SP500_ANCHOR}},
        markets=[
            ExactMarket(
                platform_ref=PolymarketRef(polymarket_id="NOPRICE"),
                mapping=LevelAtDateMapping(
                    series="security:SPY", threshold=7500.0, direction=Direction.ABOVE, at_date=date(2026, 12, 31)
                ),
            )
        ],
        bucket_families=[
            BucketFamily(
                family_id="degenerate",
                question="S&P 500 buckets with no liquidity",
                platform=Platform.POLYMARKET,
                series="security:SPY",
                at_date=date(2026, 12, 31),
                buckets=[
                    BucketMember(market_id="Z-LO", label="<7000", high=7000.0),
                    BucketMember(market_id="Z-HI", label=">=7000", low=7000.0),
                ],
            )
        ],
    )
    seeds = tuple(range(4))
    sampled = macro_model.sample(
        ExogenousSamplingRequest(
            horizon_months=_HORIZON,
            rollout_seeds=seeds,
            required_asset_prices=frozenset({SecurityKey(symbol=SP500_SYMBOL)}),
            required_private_equity_issuers=frozenset({IssuerId(_ISSUER)}),
        )
    )
    level_paths = build_anchored_level_paths(
        sampled,
        anchors={"security:SPY": _SP500_ANCHOR},
        requested_wire_ids=catalog.referenced_level_series(),
        rollout_count=4,
        horizon_months=_HORIZON,
    )
    clients: dict[Platform, PriceClient] = {
        Platform.POLYMARKET: _ProbClient({"NOPRICE": None, "Z-LO": 0.0, "Z-HI": 0.0})
    }
    result = await run_calibration(
        catalog,
        horizon_months=_HORIZON,
        rollout_seeds=seeds,
        price_clients=clients,
        bundle=sampled.private_equity,
        level_paths=level_paths,
    )
    assert result.clean == []  # None-priced market dropped, not scored
    assert result.surfaced == []  # ...and not surfaced either
    assert result.categorical == []  # degenerate (sum-to-zero) family dropped


def test_wilson_interval_edges() -> None:
    assert all(math.isnan(x) for x in wilson_interval(0, 0))
    lo, hi = wilson_interval(5, 10)
    # p_hat = 0.5 -> the 95% Wilson interval is symmetric about 0.5 and strictly inside (0, 1).
    assert 0.0 < lo < 0.5 < hi < 1.0
    assert math.isclose((lo + hi) / 2, 0.5, abs_tol=1e-9)


async def test_multi_platform_dispatches_to_correct_client(model: ConstantFrameModel) -> None:
    """Kalshi + Manifold markets each hit their own client and carry the right platform tag."""
    catalog = MarketCatalog(
        metadata={"as_of": "2026-05-29"},
        markets=[
            ExactMarket(
                platform_ref=ManifoldRef(manifold_id="M1"),
                resolution_deadline=date(2027, 1, 1),
                mapping=IpoByDateMapping(issuer=_ISSUER, by_date=date(2027, 1, 1)),
            ),
            ExactMarket(
                platform_ref=KalshiRef(kalshi_id="KXIPOOPENAI-26SEP01"),
                resolution_deadline=date(2026, 9, 1),
                mapping=IpoByDateMapping(issuer=_ISSUER, by_date=date(2026, 9, 1)),
            ),
        ],
    )
    clients = mock_price_clients({Platform.MANIFOLD: {"M1": 0.75}, Platform.KALSHI: {"KXIPOOPENAI-26SEP01": 0.50}})
    seeds = tuple(range(4))
    bundle = sample_private_equity_bundle(model, issuer=_ISSUER, horizon_months=_HORIZON, rollout_seeds=seeds)
    result = await run_calibration(
        catalog, horizon_months=_HORIZON, rollout_seeds=seeds, price_clients=clients, bundle=bundle
    )
    by_id = {row.market_id: row for row in result.clean}
    assert by_id["M1"].platform == "manifold"
    assert by_id["M1"].p_market == 0.75
    assert by_id["KXIPOOPENAI-26SEP01"].platform == "kalshi"
    assert by_id["KXIPOOPENAI-26SEP01"].p_market == 0.50


if __name__ == "__main__":
    pytest_bazel.main()
