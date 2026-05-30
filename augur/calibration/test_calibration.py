"""End-to-end `run_calibration` against a fixed-output fixture model (no network).

Uses the shared `ConstantFrameExogenousModel` fixture (augur.model.testing) seeded
with a per-rollout event array, and a `mock_manifold_client` so prices are deterministic
and hermetic.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import numpy.typing as npt
import pytest_bazel

from augur.calibration.calibration import mark_fan, run_calibration, sample_private_equity_bundle, wilson_interval
from augur.calibration.catalog import CorrelateMarket, ExactMarket, MarketCatalog
from augur.calibration.manifold import ManifoldClient
from augur.calibration.testing import mock_manifold_client
from augur.model.exogenous import ExogenousSamplingRequest
from augur.model.private_equity_bundle import PrivateEquityFloatChannel
from augur.model.series import IssuerId, PrivateEquityEventKindCode
from augur.model.testing import ConstantFrameExogenousModel, PrivateEquityChannels

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


def _model() -> ConstantFrameExogenousModel:
    return ConstantFrameExogenousModel(
        private_equity={
            IssuerId(_ISSUER): PrivateEquityChannels(mark_usd_per_unit=50.0, event_kind_code=_event_kind_codes)
        }
    )


def _catalog() -> MarketCatalog:
    """One exact ipo_by_date, one exact pre_ipo_failure, one correlate (ipo_by_date) market."""
    return MarketCatalog(
        metadata={"as_of": "2026-05-29", "augur_model_as_of": "2026-05-27"},
        markets=[
            ExactMarket(
                slug="ipo-before-2027",
                manifold_id="AAA",
                question="Issuer IPO before 2027?",
                outcome_type="BINARY",
                resolution_deadline=date(2027, 1, 1),
                mapping_kind="ipo_by_date",
                mapping_params={"by_date": "2027-01-01"},
            ),
            ExactMarket(
                slug="collapse-before-ipo",
                manifold_id="BBB",
                question="Issuer collapses or acquired before IPO?",
                outcome_type="BINARY",
                mapping_kind="pre_ipo_failure",
                mapping_params={"before_event": "PUBLIC_MARKET_OPEN"},
            ),
            CorrelateMarket(
                slug="valuation-1t",
                manifold_id="CCC",
                question="Issuer completes an IPO in 2026 with $1T cap?",
                outcome_type="BINARY",
                resolution_deadline=date(2026, 12, 31),
                correlate_of="ipo_by_date",
                correlate_strength="strong",
                reason="The >=$1T cap conjunct needs a valuation augur does not model.",
            ),
        ],
    )


def _prices() -> ManifoldClient:
    return mock_manifold_client({"AAA": 0.40, "BBB": 0.10, "CCC": 0.66})


def _run():
    return run_calibration(
        _model(),
        _catalog(),
        issuer=_ISSUER,
        horizon_months=_HORIZON,
        rollout_seeds=tuple(range(4)),
        price_client=_prices(),
    )


def test_clean_rows_score_events() -> None:
    result = _run()
    assert result.issuer == _ISSUER
    assert result.rollout_count == 4
    clean = {row.slug: row for row in result.clean}

    # ipo_by_date(2027-01-01 -> month 7): rollout 0 YES; rollouts 1,3 NO (no IPO by then,
    # whole horizon simulated); rollout 2 NO (collapsed, no IPO). All resolved.
    ipo = clean["ipo-before-2027"]
    assert ipo.n_resolved == 4
    assert ipo.unresolved == 0
    assert ipo.p_model == 0.25
    assert ipo.p_market == 0.40  # injected stub price
    assert ipo.abs_gap is not None
    assert math.isclose(ipo.abs_gap, 0.15)

    # pre_ipo_failure: rollout 2 YES (collapse before IPO); rollouts 0,1 NO (IPO first);
    # rollout 3 UNRESOLVED (still private at horizon end).
    fail = clean["collapse-before-ipo"]
    assert fail.n_resolved == 3
    assert fail.unresolved == 1
    assert fail.p_model is not None
    assert math.isclose(fail.p_model, 1 / 3)


def test_surfaced_row_carries_augur_context() -> None:
    result = _run()
    assert [row.slug for row in result.surfaced] == ["valuation-1t"]
    surfaced = result.surfaced[0]
    assert surfaced.correlate_of == "ipo_by_date"
    assert surfaced.p_market == 0.66  # injected stub price
    assert surfaced.reason == "The >=$1T cap conjunct needs a valuation augur does not model."
    # deadline 2026-12-31 is month 7 from as_of 2026-05-27; rollout 0 IPOs at month 7 (<=7),
    # the others don't -> P(IPO by deadline) = 1/4. This is surfaced context, NOT a score.
    assert surfaced.augur_context is not None
    assert surfaced.augur_context.signal == "P(PUBLIC_MARKET_OPEN by deadline)"
    assert surfaced.augur_context.p_model == 0.25


def test_run_calibration_reuses_supplied_bundle() -> None:
    # Sampling the bundle once and threading it into run_calibration must yield the same
    # scoring as letting run_calibration sample internally -- the backend reuses one bundle
    # for both the clean/surfaced scoring and the mark_fan.
    catalog = _catalog()
    seeds = tuple(range(4))
    bundle = sample_private_equity_bundle(_model(), issuer=_ISSUER, horizon_months=_HORIZON, rollout_seeds=seeds)
    from_bundle = run_calibration(
        _model(),
        catalog,
        issuer=_ISSUER,
        horizon_months=_HORIZON,
        rollout_seeds=seeds,
        price_client=_prices(),
        bundle=bundle,
    )
    internal = run_calibration(
        _model(), catalog, issuer=_ISSUER, horizon_months=_HORIZON, rollout_seeds=seeds, price_client=_prices()
    )
    assert from_bundle == internal
    # The same bundle also drives the mark_fan, so both views come from one rollout.
    fan = mark_fan(bundle, issuer=_ISSUER, rollout_count=4, horizon_months=_HORIZON, percentiles=(50.0,))
    assert fan.months[0].values == {"50.0": 50.0}


def test_mark_fan_shape() -> None:
    request = ExogenousSamplingRequest(
        horizon_months=_HORIZON,
        rollout_seeds=tuple(range(4)),
        required_private_equity_issuers=frozenset({IssuerId(_ISSUER)}),
    )
    bundle = _model().sample(request).private_equity
    fan = mark_fan(bundle, issuer=_ISSUER, rollout_count=4, horizon_months=_HORIZON, percentiles=(5.0, 50.0, 95.0))
    assert fan.channel == PrivateEquityFloatChannel.MARK_USD_PER_UNIT
    assert fan.percentiles == [5.0, 50.0, 95.0]
    assert len(fan.months) == _HORIZON + 1
    # Constant 50.0 mark -> every percentile band is 50.0.
    assert fan.months[0].values == {"5.0": 50.0, "50.0": 50.0, "95.0": 50.0}


def test_wilson_interval_edges() -> None:
    assert all(math.isnan(x) for x in wilson_interval(0, 0))
    lo, hi = wilson_interval(5, 10)
    # p_hat = 0.5 -> the 95% Wilson interval is symmetric about 0.5 and strictly inside (0, 1).
    assert 0.0 < lo < 0.5 < hi < 1.0
    assert math.isclose((lo + hi) / 2, 0.5, abs_tol=1e-9)


if __name__ == "__main__":
    pytest_bazel.main()
