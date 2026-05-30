from __future__ import annotations

import math

import numpy as np
import numpy.typing as npt
import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from augur.model.exogenous import ExogenousSamplingRequest, SampledExogenousBundle
from augur.model.exogenous_provider_config import ExogenousProviderConfig
from augur.model.private_equity_bundle import (
    PrivateEquityBoolChannel,
    PrivateEquityFloatChannel,
    PrivateEquityIntChannel,
)
from augur.model.private_equity_risk import (
    PrivateEquityRiskIssuerConfig,
    PrivateEquityRiskProviderConfig,
    PublicMarketCdfAnchor,
    _public_market_open_hazard_by_month,
)
from augur.model.series import IssuerId, PrivateEquityEventKindCode, PrivateEquityRegimeCode


def _issuer(**updates: object) -> PrivateEquityRiskIssuerConfig:
    fields = {
        "current_mark_usd": 100.0,
        "tender_interval_months_median": 120.0,
        "tender_interval_log_sigma": 0.0,
        **updates,
    }
    return PrivateEquityRiskIssuerConfig.model_validate(fields)


def _sample(issuer: PrivateEquityRiskIssuerConfig, *, horizon_months: int = 4) -> SampledExogenousBundle:
    model = PrivateEquityRiskProviderConfig(issuers={"acme": issuer}).realize_model()
    return model.sample(
        ExogenousSamplingRequest(
            horizon_months=horizon_months,
            rollout_seeds=(7,),
            required_private_equity_issuers=frozenset({IssuerId("acme")}),
        )
    )


def _float(sampled: SampledExogenousBundle, channel: PrivateEquityFloatChannel, horizon: int) -> np.ndarray:
    return sampled.private_equity.issuer_float_matrix("acme", str(channel), rollout_count=1, horizon_months=horizon)


def _int(sampled: SampledExogenousBundle, channel: PrivateEquityIntChannel, horizon: int) -> np.ndarray:
    return sampled.private_equity.issuer_int_matrix("acme", str(channel), rollout_count=1, horizon_months=horizon)


def _bool(sampled: SampledExogenousBundle, channel: PrivateEquityBoolChannel, horizon: int) -> np.ndarray:
    return sampled.private_equity.issuer_bool_matrix("acme", str(channel), rollout_count=1, horizon_months=horizon)


def test_private_equity_risk_provider_config_roundtrips_through_union() -> None:
    adapter: TypeAdapter[ExogenousProviderConfig] = TypeAdapter(ExogenousProviderConfig)
    config = adapter.validate_python({"type": "private_equity_risk", "issuers": {"acme": {"current_mark_usd": 100.0}}})

    assert isinstance(config, PrivateEquityRiskProviderConfig)
    assert config.realize_model().sample(ExogenousSamplingRequest(horizon_months=1, rollout_seeds=(1,))).metadata[
        "private_equity_prices_usd"
    ] == {"acme": 100.0}


def test_private_equity_risk_samples_complete_protocol_bundle() -> None:
    sampled = _sample(_issuer(), horizon_months=2)

    assert sampled.private_equity.issuer_ids() == frozenset({"acme"})
    np.testing.assert_allclose(
        _float(sampled, PrivateEquityFloatChannel.MARK_USD_PER_UNIT, horizon=2), np.array([[100.0, 100.0, 100.0]])
    )


def test_private_equity_risk_private_mark_is_piecewise_constant_between_observed_ticks() -> None:
    sampled = _sample(_issuer(monthly_log_return_mu=math.log(2.0), monthly_log_return_sigma=0.0), horizon_months=4)

    np.testing.assert_allclose(
        _float(sampled, PrivateEquityFloatChannel.MARK_USD_PER_UNIT, horizon=4), np.full((1, 5), 100.0)
    )


def test_private_equity_risk_admin_mark_update_changes_mark_without_sale_opportunity() -> None:
    sampled = _sample(
        _issuer(
            monthly_log_return_mu=math.log(2.0) / 2.0,
            monthly_log_return_sigma=0.0,
            admin_mark_update_interval_months_median=2.0,
            admin_mark_update_interval_log_sigma=0.0,
            admin_mark_update_log_noise_sigma=0.0,
        ),
        horizon_months=4,
    )

    mark = _float(sampled, PrivateEquityFloatChannel.MARK_USD_PER_UNIT, horizon=4)
    event_kind = _int(sampled, PrivateEquityIntChannel.EVENT_KIND_CODE, horizon=4)
    tenders = _bool(sampled, PrivateEquityBoolChannel.SALE_OPPORTUNITY_ACTIVE, horizon=4)

    assert int(event_kind[0, 2]) == int(PrivateEquityEventKindCode.ADMIN_MARK_UPDATE)
    assert int(event_kind[0, 4]) == int(PrivateEquityEventKindCode.ADMIN_MARK_UPDATE)
    np.testing.assert_array_equal(tenders, np.zeros((1, 5), dtype=np.bool_))
    assert mark[0, 0] == pytest.approx(100.0)
    assert mark[0, 1] == pytest.approx(100.0)
    assert mark[0, 2] == pytest.approx(200.0)
    assert mark[0, 3] == pytest.approx(200.0)
    assert mark[0, 4] == pytest.approx(400.0)


def test_private_equity_risk_tender_updates_mark_and_sale_opportunity() -> None:
    sampled = _sample(
        _issuer(
            monthly_log_return_mu=math.log(2.0) / 2.0,
            monthly_log_return_sigma=0.0,
            tender_interval_months_median=2.0,
            tender_interval_log_sigma=0.0,
            tender_price_log_discount_sigma=0.0,
        ),
        horizon_months=3,
    )

    mark = _float(sampled, PrivateEquityFloatChannel.MARK_USD_PER_UNIT, horizon=3)
    event_kind = _int(sampled, PrivateEquityIntChannel.EVENT_KIND_CODE, horizon=3)
    tenders = _bool(sampled, PrivateEquityBoolChannel.SALE_OPPORTUNITY_ACTIVE, horizon=3)

    assert int(event_kind[0, 2]) == int(PrivateEquityEventKindCode.TENDER)
    assert tenders[0, 2]
    assert mark[0, 0] == pytest.approx(100.0)
    assert mark[0, 1] == pytest.approx(100.0)
    assert mark[0, 2] == pytest.approx(200.0)
    assert mark[0, 3] == pytest.approx(200.0)


def test_private_equity_risk_forced_recovery_cashout_marks_protocol_event() -> None:
    sampled = _sample(
        _issuer(
            annual_forced_recovery_probability=1.0,
            forced_recovery_cashout_usd_min=100.0,
            forced_recovery_cashout_usd_max=100.0,
        ),
        horizon_months=3,
    )

    recovery = _float(sampled, PrivateEquityFloatChannel.FORCED_RECOVERY_CASHOUT_USD, horizon=3)
    event_kind = _int(sampled, PrivateEquityIntChannel.EVENT_KIND_CODE, horizon=3)
    regime = _int(sampled, PrivateEquityIntChannel.REGIME_CODE, horizon=3)
    blocked = _bool(sampled, PrivateEquityBoolChannel.LIQUIDITY_BLOCKED, horizon=3)

    assert recovery[0, 1] == pytest.approx(100.0)
    assert int(event_kind[0, 1]) == int(PrivateEquityEventKindCode.FORCED_RECOVERY)
    np.testing.assert_array_equal(regime[0, 1:], np.full(3, int(PrivateEquityRegimeCode.COLLAPSED)))
    np.testing.assert_array_equal(blocked[0, 1:], np.ones(3, dtype=np.bool_))


def test_private_equity_risk_collapse_blocks_liquidity_and_marks_down() -> None:
    sampled = _sample(_issuer(annual_collapse_probability=1.0, collapsed_mark_fraction=0.01), horizon_months=3)

    mark = _float(sampled, PrivateEquityFloatChannel.MARK_USD_PER_UNIT, horizon=3)
    event_kind = _int(sampled, PrivateEquityIntChannel.EVENT_KIND_CODE, horizon=3)
    regime = _int(sampled, PrivateEquityIntChannel.REGIME_CODE, horizon=3)
    blocked = _bool(sampled, PrivateEquityBoolChannel.LIQUIDITY_BLOCKED, horizon=3)

    assert int(event_kind[0, 1]) == int(PrivateEquityEventKindCode.COLLAPSE)
    np.testing.assert_array_equal(regime[0, 1:], np.full(3, int(PrivateEquityRegimeCode.COLLAPSED)))
    np.testing.assert_array_equal(blocked[0, 1:], np.ones(3, dtype=np.bool_))
    np.testing.assert_allclose(mark[0, 1:], np.full(3, 1.0))


def test_private_equity_risk_public_market_is_absorbing_open_liquidity_regime() -> None:
    sampled = _sample(_issuer(annual_public_market_probability=1.0), horizon_months=3)

    event_kind = _int(sampled, PrivateEquityIntChannel.EVENT_KIND_CODE, horizon=3)
    regime = _int(sampled, PrivateEquityIntChannel.REGIME_CODE, horizon=3)
    blocked = _bool(sampled, PrivateEquityBoolChannel.LIQUIDITY_BLOCKED, horizon=3)

    assert int(event_kind[0, 1]) == int(PrivateEquityEventKindCode.PUBLIC_MARKET_OPEN)
    np.testing.assert_array_equal(regime[0, 1:], np.full(3, int(PrivateEquityRegimeCode.PUBLIC_MARKET)))
    np.testing.assert_array_equal(blocked[0, 1:], np.zeros(3, dtype=np.bool_))


def test_private_equity_risk_public_market_lockup_blocks_liquidity_then_opens() -> None:
    sampled = _sample(_issuer(annual_public_market_probability=1.0, public_market_lockup_months=2), horizon_months=4)

    regime = _int(sampled, PrivateEquityIntChannel.REGIME_CODE, horizon=4)
    blocked = _bool(sampled, PrivateEquityBoolChannel.LIQUIDITY_BLOCKED, horizon=4)

    np.testing.assert_array_equal(regime[0, 1:], np.full(4, int(PrivateEquityRegimeCode.PUBLIC_MARKET)))
    np.testing.assert_array_equal(blocked[0], np.array([False, True, True, False, False], dtype=np.bool_))


def test_private_equity_risk_forced_sale_emits_sale_fraction_without_tender() -> None:
    sampled = _sample(
        _issuer(annual_forced_sale_probability=1.0, forced_sale_fraction_alpha=1000.0, forced_sale_fraction_beta=1.0),
        horizon_months=2,
    )

    event_kind = _int(sampled, PrivateEquityIntChannel.EVENT_KIND_CODE, horizon=2)
    forced_sale = _float(sampled, PrivateEquityFloatChannel.FORCED_SALE_FRACTION, horizon=2)
    tenders = _bool(sampled, PrivateEquityBoolChannel.SALE_OPPORTUNITY_ACTIVE, horizon=2)
    regime = _int(sampled, PrivateEquityIntChannel.REGIME_CODE, horizon=2)

    assert int(event_kind[0, 1]) == int(PrivateEquityEventKindCode.ACQUISITION_CASHOUT)
    assert int(regime[0, 1]) == int(PrivateEquityRegimeCode.ACQUIRED)
    assert 0.0 < forced_sale[0, 1] <= 1.0
    assert not tenders[0, 1]


def test_private_equity_risk_tender_cancellation_suppresses_scheduled_tender() -> None:
    """A scheduled tender precursor with cancellation=1.0 fires no TENDER event."""

    sampled = _sample(
        _issuer(
            monthly_log_return_mu=0.0,
            monthly_log_return_sigma=0.0,
            tender_interval_months_median=2.0,
            tender_interval_log_sigma=0.0,
            tender_cancellation_probability=1.0,
        ),
        horizon_months=3,
    )

    event_kind = _int(sampled, PrivateEquityIntChannel.EVENT_KIND_CODE, horizon=3)
    tenders = _bool(sampled, PrivateEquityBoolChannel.SALE_OPPORTUNITY_ACTIVE, horizon=3)
    sale_capacity = _float(sampled, PrivateEquityFloatChannel.SALE_CAPACITY_FRACTION, horizon=3)

    np.testing.assert_array_equal(event_kind[0, :], np.zeros(4, dtype=np.int64))
    np.testing.assert_array_equal(tenders[0, :], np.zeros(4, dtype=np.bool_))
    np.testing.assert_array_equal(sale_capacity[0, :], np.zeros(4, dtype=np.float64))


def test_private_equity_risk_tender_cancellation_default_zero_preserves_tender() -> None:
    """Cancellation defaults to 0.0 — scheduled tender fires as before."""

    sampled = _sample(
        _issuer(
            monthly_log_return_mu=0.0,
            monthly_log_return_sigma=0.0,
            tender_interval_months_median=2.0,
            tender_interval_log_sigma=0.0,
            tender_price_log_discount_sigma=0.0,
        ),
        horizon_months=3,
    )

    event_kind = _int(sampled, PrivateEquityIntChannel.EVENT_KIND_CODE, horizon=3)
    tenders = _bool(sampled, PrivateEquityBoolChannel.SALE_OPPORTUNITY_ACTIVE, horizon=3)

    assert int(event_kind[0, 2]) == int(PrivateEquityEventKindCode.TENDER)
    assert tenders[0, 2]


def test_private_equity_risk_legal_event_severity_matches_plan_80_15_5_split() -> None:
    """Umbrella legal_event probability splits into 80%/15%/5% per the realization-risk plan.

    With `annual_legal_event_probability=1.0` every eligible rollout fires a legal
    event on month 1. Severity is then a function of u_legal_severity uniform draws.
    Over many rollouts the 5% severe branch (which emits LEGAL_IMPAIRMENT) and the
    80% temporary branch (NONE event_kind, suspended for at least the firing month)
    should land within sampling tolerance of the plan-specified shares.
    """

    rollout_count = 4096
    rollout_seeds = tuple(range(1701, 1701 + rollout_count))
    issuer = _issuer(
        monthly_log_return_mu=0.0,
        monthly_log_return_sigma=0.0,
        # Push tender precursor far enough out that the month-1 legal event always wins.
        tender_interval_months_median=120.0,
        annual_legal_event_probability=1.0,
    )
    model = PrivateEquityRiskProviderConfig(issuers={"acme": issuer}).realize_model()
    sampled = model.sample(
        ExogenousSamplingRequest(
            horizon_months=1, rollout_seeds=rollout_seeds, required_private_equity_issuers=frozenset({IssuerId("acme")})
        )
    )
    event_kind = sampled.private_equity.issuer_int_matrix(
        "acme", str(PrivateEquityIntChannel.EVENT_KIND_CODE), rollout_count=rollout_count, horizon_months=1
    )

    legal_share = float((event_kind[:, 1] == int(PrivateEquityEventKindCode.LEGAL_IMPAIRMENT)).mean())
    none_share = float((event_kind[:, 1] == int(PrivateEquityEventKindCode.NONE)).mean())
    # All eligible rollouts must take one of the two outcomes — no other event_kind
    # should fire when the umbrella legal_event rate is 1.0.
    assert legal_share + none_share == pytest.approx(1.0)
    # 5% severe → emits LEGAL_IMPAIRMENT.
    assert 0.03 <= legal_share <= 0.07
    # 80% temp + 15% perm-cap → both NONE event_kind, summing to 95%.
    assert 0.93 <= none_share <= 0.97


def test_private_equity_risk_legal_impairment_severe_indefinite_blocks_at_firing_month() -> None:
    """Of the rollouts that fire LEGAL_IMPAIRMENT, the 50% indefinite sub-branch
    sets `liquidity_blocked = 1` starting at the firing month; the 30%
    near-zero-capacity and 20% small-dollar-recovery sub-branches leave it at 0.
    Using horizon=1 avoids compounding from subsequent months' legal events.
    """

    rollout_count = 8192
    rollout_seeds = tuple(range(2001, 2001 + rollout_count))
    issuer = _issuer(
        monthly_log_return_mu=0.0,
        monthly_log_return_sigma=0.0,
        tender_interval_months_median=120.0,
        annual_legal_event_probability=1.0,
    )
    model = PrivateEquityRiskProviderConfig(issuers={"acme": issuer}).realize_model()
    sampled = model.sample(
        ExogenousSamplingRequest(
            horizon_months=1, rollout_seeds=rollout_seeds, required_private_equity_issuers=frozenset({IssuerId("acme")})
        )
    )
    event_kind = sampled.private_equity.issuer_int_matrix(
        "acme", str(PrivateEquityIntChannel.EVENT_KIND_CODE), rollout_count=rollout_count, horizon_months=1
    )
    blocked = sampled.private_equity.issuer_bool_matrix(
        "acme", str(PrivateEquityBoolChannel.LIQUIDITY_BLOCKED), rollout_count=rollout_count, horizon_months=1
    )

    severe_mask = event_kind[:, 1] == int(PrivateEquityEventKindCode.LEGAL_IMPAIRMENT)
    severe_count = int(severe_mask.sum())
    assert severe_count >= 150, f"too few severe rollouts ({severe_count}) for a reliable subdistribution test"
    indefinite_share = float(blocked[severe_mask, 1].mean())
    # Plan's severe sub-split: 50% indefinite (blocked), 30% near-zero cap + 20% small recovery (not blocked).
    assert 0.40 <= indefinite_share <= 0.60


def test_private_equity_risk_unrequested_issuer_still_satisfies_request() -> None:
    """The PE risk model samples its configured issuers regardless of what's
    explicitly requested; requesting an unknown issuer fails at validation."""

    model = PrivateEquityRiskProviderConfig(issuers={"acme": _issuer()}).realize_model()

    with pytest.raises(ValueError, match=r"missing required private-equity issuer\(s\): \['other_issuer'\]"):
        model.sample(
            ExogenousSamplingRequest(
                horizon_months=1,
                rollout_seeds=(1,),
                required_private_equity_issuers=frozenset({IssuerId("other_issuer")}),
            )
        )


# Empirical going-public CDF prior — prediction-market-derived IPO anchors.
_IPO_ANCHORS: tuple[PublicMarketCdfAnchor, ...] = (
    PublicMarketCdfAnchor(month=7, cumulative_probability=0.75),
    PublicMarketCdfAnchor(month=19, cumulative_probability=0.89),
    PublicMarketCdfAnchor(month=31, cumulative_probability=0.93),
)


def test_public_market_open_hazard_reproduces_anchor_cdf() -> None:
    """The hazard builder turns CDF anchors into per-bucket constant monthly hazards
    whose compounded survival hits each anchor's cumulative probability exactly."""

    issuer = _issuer(public_market_cdf_anchors=_IPO_ANCHORS, annual_public_market_probability=0.07)
    hazard: npt.NDArray[np.float64] = _public_market_open_hazard_by_month(issuer, horizon_months=36)

    # First three buckets are (0,7], (7,19], (19,31]; their constant monthly hazards
    # are the front-loaded, saturating shape the prediction market implies.
    assert hazard[1] == pytest.approx(0.18, abs=2e-3)
    assert hazard[8] == pytest.approx(0.067, abs=2e-3)
    assert hazard[20] == pytest.approx(0.037, abs=2e-3)

    # Index 0 is unused by the sampler (the per-month loop runs t>=1), so the CDF the
    # model realizes is the compounded survival over months 1..N. Reproduce that.
    cumulative: npt.NDArray[np.float64] = np.zeros_like(hazard)
    cumulative[1:] = 1.0 - np.cumprod(1.0 - hazard[1:])
    assert cumulative[7] == pytest.approx(0.75, abs=1e-9)
    assert cumulative[19] == pytest.approx(0.89, abs=1e-9)
    assert cumulative[31] == pytest.approx(0.93, abs=1e-9)

    # Past the last anchor the flat annual tail hazard takes over.
    assert hazard[32] == pytest.approx(1.0 - (1.0 - 0.07) ** (1.0 / 12.0), abs=1e-12)


def test_public_market_open_hazard_without_anchors_is_flat_tail() -> None:
    """No anchors → the whole vector is the legacy flat monthly hazard."""

    issuer = _issuer(annual_public_market_probability=0.07)
    hazard = _public_market_open_hazard_by_month(issuer, horizon_months=12)

    flat = 1.0 - (1.0 - 0.07) ** (1.0 / 12.0)
    np.testing.assert_allclose(hazard, np.full(13, flat))


def test_public_market_cdf_anchors_reject_non_increasing_month() -> None:
    with pytest.raises(ValidationError, match="strictly increasing in month"):
        _issuer(
            public_market_cdf_anchors=(
                PublicMarketCdfAnchor(month=7, cumulative_probability=0.5),
                PublicMarketCdfAnchor(month=7, cumulative_probability=0.6),
            )
        )


def test_public_market_cdf_anchors_reject_decreasing_cumulative_probability() -> None:
    with pytest.raises(ValidationError, match="non-decreasing in cumulative_probability"):
        _issuer(
            public_market_cdf_anchors=(
                PublicMarketCdfAnchor(month=7, cumulative_probability=0.6),
                PublicMarketCdfAnchor(month=19, cumulative_probability=0.5),
            )
        )


def test_public_market_open_realized_probability_tracks_anchor_cdf() -> None:
    """Integration: with the empirical anchors and all competing adverse hazards ≈0,
    the realized fraction of rollouts that have gone public by months 7/19/31 matches
    the prediction-market CDF (0.75/0.89/0.93) within Monte-Carlo tolerance.
    """

    rollout_count = 6000
    rollout_seeds = tuple(range(5000, 5000 + rollout_count))
    issuer = _issuer(
        monthly_log_return_mu=0.0,
        monthly_log_return_sigma=0.0,
        # Push the tender precursor far past the horizon so it never preempts a month.
        tender_interval_months_median=600.0,
        public_market_cdf_anchors=_IPO_ANCHORS,
        annual_public_market_probability=0.07,
    )
    model = PrivateEquityRiskProviderConfig(issuers={"acme": issuer}).realize_model()
    sampled = model.sample(
        ExogenousSamplingRequest(
            horizon_months=31,
            rollout_seeds=rollout_seeds,
            required_private_equity_issuers=frozenset({IssuerId("acme")}),
        )
    )
    regime = sampled.private_equity.issuer_int_matrix(
        "acme", str(PrivateEquityIntChannel.REGIME_CODE), rollout_count=rollout_count, horizon_months=31
    )
    public = regime == int(PrivateEquityRegimeCode.PUBLIC_MARKET)

    # PUBLIC_MARKET is absorbing, so "public by month m" == public regime at month m.
    realized = {m: float(public[:, m].mean()) for m in (7, 19, 31)}
    assert realized[7] == pytest.approx(0.75, abs=0.02)
    assert realized[19] == pytest.approx(0.89, abs=0.02)
    assert realized[31] == pytest.approx(0.93, abs=0.02)


if __name__ == "__main__":
    pytest_bazel.main()
