from __future__ import annotations

import math

import numpy as np
import numpy.typing as npt
import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from augur.model.exogenous import ExogenousSamplingRequest, SampledExogenousBundle
from augur.model.private_equity_bundle import (
    PrivateEquityBoolChannel,
    PrivateEquityFloatChannel,
    PrivateEquityIntChannel,
)
from augur.model.private_equity_risk import (
    PrivateEquityRiskIssuerConfig,
    PrivateEquityRiskProviderConfig,
    PublicMarketCdfAnchor,
    _dilution_factor,
    _public_market_open_hazard_by_month,
    _sample_company_valuation_vectorized,
    _sample_issuer,
    _seed_from_rollout_seeds,
)
from augur.model.provider_config import ProviderConfig
from augur.model.series import IssuerId, PrivateEquityEventKindCode, PrivateEquityRegimeCode
from augur.model.series_model import derive_stream_rollout_seeds


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
    adapter: TypeAdapter[ProviderConfig] = TypeAdapter(ProviderConfig)
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


def _valuation_issuer(**updates: object) -> PrivateEquityRiskIssuerConfig:
    """An issuer with the opt-in M2 coupled valuation+dilution channel enabled.

    Adverse hazards are all left at their (zero) defaults so the only thing moving
    the mark is the coupled V(t)/dilution machinery — keeps the assertions clean.
    """

    return _issuer(
        current_valuation_usd=1.0e11,
        shares_outstanding_initial=1.0e9,
        valuation_monthly_log_return_mu=0.02,
        valuation_monthly_log_return_sigma=0.10,
        valuation_student_t_nu=5.0,
        # No legacy latent-mark drift/vol: when the channel is ON these are unused,
        # but keeping them inert makes the channel-OFF baseline below unambiguous.
        monthly_log_return_mu=0.0,
        monthly_log_return_sigma=0.0,
        # Push tender/admin precursors past the horizon so mark[:,0] and the coupled
        # path aren't perturbed by event-price noise in the first columns.
        tender_interval_months_median=600.0,
        **updates,
    )


def test_valuation_channel_off_by_default() -> None:
    """`current_valuation_usd` unset ⇒ channel off ⇒ `company_valuation_usd` all-zeros."""

    sampled = _sample(_issuer(), horizon_months=4)
    valuation = _float(sampled, PrivateEquityFloatChannel.COMPANY_VALUATION_USD, horizon=4)
    np.testing.assert_array_equal(valuation, np.zeros((1, 5), dtype=np.float64))
    assert not _issuer().valuation_channel_enabled


def test_valuation_channel_on_anchors_columns_zero() -> None:
    """Channel ON: valuation[:,0] == V0 and mark[:,0] == current_mark_usd exactly."""

    issuer = _valuation_issuer()
    assert issuer.valuation_channel_enabled
    sampled = _sample(issuer, horizon_months=6)

    valuation = _float(sampled, PrivateEquityFloatChannel.COMPANY_VALUATION_USD, horizon=6)
    mark = _float(sampled, PrivateEquityFloatChannel.MARK_USD_PER_UNIT, horizon=6)

    np.testing.assert_array_equal(valuation[:, 0], np.full(valuation.shape[0], 1.0e11))
    assert mark[0, 0] == pytest.approx(100.0)
    # The coupled valuation is a strictly positive market cap, never the all-zeros sentinel.
    assert np.all(valuation > 0.0)


def test_valuation_channel_on_is_deterministic_under_fixed_seeds() -> None:
    """Two samples with identical `rollout_seeds` produce identical coupled arrays."""

    issuer = _valuation_issuer()
    request = ExogenousSamplingRequest(
        horizon_months=8, rollout_seeds=(11, 22, 33), required_private_equity_issuers=frozenset({IssuerId("acme")})
    )
    model = PrivateEquityRiskProviderConfig(issuers={"acme": issuer}).realize_model()
    first = model.sample(request)
    second = model.sample(request)

    for channel in (PrivateEquityFloatChannel.COMPANY_VALUATION_USD, PrivateEquityFloatChannel.MARK_USD_PER_UNIT):
        a = first.private_equity.issuer_float_matrix("acme", str(channel), rollout_count=3, horizon_months=8)
        b = second.private_equity.issuer_float_matrix("acme", str(channel), rollout_count=3, horizon_months=8)
        np.testing.assert_array_equal(a, b)


def _deterministic_dilution_factor(*, rate: float, horizon_months: int) -> np.ndarray:
    """The M2.2-A per-rollout `_dilution_factor` at sigma=0 over a single rollout, as a
    `(T+1,)` row -- i.e. the legacy deterministic factor recovered through the unified path."""

    return np.asarray(
        _dilution_factor(
            annual_dilution_rate=rate,
            annual_dilution_rate_log_sigma=0.0,
            rollout_seeds=(7,),
            issuer_id="acme",
            rollout_count=1,
            horizon_months=horizon_months,
        )[0]
    )


def test_dilution_factor_shape_and_values() -> None:
    """`dilution_factor(t) = (1+rate)^(t/12)`, with t=0 ⇒ 1 and t=12 ⇒ 1+rate."""

    rate = 0.30
    factor = _deterministic_dilution_factor(rate=rate, horizon_months=24)
    assert factor.shape == (25,)
    assert factor[0] == pytest.approx(1.0)
    assert factor[12] == pytest.approx(1.0 + rate)
    assert factor[24] == pytest.approx((1.0 + rate) ** 2)
    # Zero dilution ⇒ identity row.
    np.testing.assert_allclose(_deterministic_dilution_factor(rate=0.0, horizon_months=6), np.ones(7))


def test_positive_dilution_makes_coupled_mark_grow_slower_than_valuation_ratio() -> None:
    """`annual_dilution_rate > 0` ⇒ the coupled latent mark grows strictly slower than V(t)/V0.

    The coupled latent mark is `current_mark × (V(t)/V0) / dilution_factor(t)` with
    `dilution_factor(t) = (1+rate)^(t/12) > 1` for t > 0. Verified directly on the
    sampler's building blocks (`_sample_company_valuation_vectorized` + `_dilution_factor`),
    since the observed `mark_usd_per_unit` channel is piecewise-constant between observation
    events and so doesn't continuously track the latent mark. Same V(t) used for both rates
    (identical valuation seed stream), so the only difference is the dilution divisor.
    """

    rate = 0.30
    horizon = 12
    issuer = _valuation_issuer(annual_dilution_rate=rate)
    valuation_seeds = (900, 901, 902, 903)
    valuation = _sample_company_valuation_vectorized(issuer, valuation_seeds=valuation_seeds, horizon_months=horizon)
    valuation_ratio = valuation / valuation[:, [0]]

    diluted = _deterministic_dilution_factor(rate=rate, horizon_months=horizon)
    undiluted = _deterministic_dilution_factor(rate=0.0, horizon_months=horizon)
    coupled_mark = issuer.current_mark_usd * valuation_ratio / diluted
    coupled_mark_no_dilution = issuer.current_mark_usd * valuation_ratio / undiluted

    mark_ratio = coupled_mark / coupled_mark[:, [0]]
    # t == 0: mark ratio equals valuation ratio (dilution_factor(0) == 1).
    np.testing.assert_allclose(mark_ratio[:, 0], valuation_ratio[:, 0])
    # t > 0: strictly below the valuation ratio, by exactly the dilution factor.
    assert np.all(mark_ratio[:, 1:] < valuation_ratio[:, 1:])
    np.testing.assert_allclose(mark_ratio, valuation_ratio / diluted)
    # Zero dilution ⇒ coupled mark tracks V(t)/V0 exactly.
    np.testing.assert_allclose(coupled_mark_no_dilution / issuer.current_mark_usd, valuation_ratio)


def test_valuation_channel_off_is_byte_identical_to_pre_m2_baseline() -> None:
    """Zero-regression guard: turning the channel off must leave mark/event arrays
    bit-identical to a model with NO valuation fields at all (the pre-M2 shape).

    Both issuers share every legacy parameter and a non-trivial mark random walk plus
    live adverse hazards, exercised over many rollouts so any perturbation of the level
    or event RNG streams (e.g. an accidentally-derived valuation seed stream) would show
    up as a difference. The valuation-fields-present-but-disabled issuer must reproduce
    the bare issuer exactly, and emit an all-zeros valuation channel.
    """

    legacy_params: dict[str, object] = {
        "monthly_log_return_mu": 0.01,
        "monthly_log_return_sigma": 0.08,
        "tender_interval_months_median": 9.0,
        "tender_interval_log_sigma": 0.3,
        "annual_public_market_probability": 0.05,
        "annual_collapse_probability": 0.02,
        "annual_legal_event_probability": 0.05,
        "annual_forced_sale_probability": 0.02,
    }
    rollout_count = 256
    rollout_seeds = tuple(range(4242, 4242 + rollout_count))
    request = ExogenousSamplingRequest(
        horizon_months=18, rollout_seeds=rollout_seeds, required_private_equity_issuers=frozenset({IssuerId("acme")})
    )

    # Pre-M2 shape: no valuation fields set at all.
    bare = PrivateEquityRiskProviderConfig(issuers={"acme": _issuer(**legacy_params)}).realize_model().sample(request)
    # M2 fields present but channel OFF (current_valuation_usd unset). Set the inert
    # valuation RW params to NON-zero values to prove they cannot leak when the channel
    # is off (their seed stream is never derived).
    disabled = (
        PrivateEquityRiskProviderConfig(
            issuers={
                "acme": _issuer(
                    valuation_monthly_log_return_mu=0.5,
                    valuation_monthly_log_return_sigma=0.5,
                    valuation_student_t_nu=3.0,
                    annual_dilution_rate=0.4,
                    **legacy_params,
                )
            }
        )
        .realize_model()
        .sample(request)
    )

    def matrix(bundle: SampledExogenousBundle, channel: PrivateEquityFloatChannel) -> np.ndarray:
        return bundle.private_equity.issuer_float_matrix(
            "acme", str(channel), rollout_count=rollout_count, horizon_months=18
        )

    def int_matrix(bundle: SampledExogenousBundle, channel: PrivateEquityIntChannel) -> np.ndarray:
        return bundle.private_equity.issuer_int_matrix(
            "acme", str(channel), rollout_count=rollout_count, horizon_months=18
        )

    # Mark and event-kind arrays must be byte-identical between the bare and disabled issuers.
    np.testing.assert_array_equal(
        matrix(bare, PrivateEquityFloatChannel.MARK_USD_PER_UNIT),
        matrix(disabled, PrivateEquityFloatChannel.MARK_USD_PER_UNIT),
    )
    np.testing.assert_array_equal(
        int_matrix(bare, PrivateEquityIntChannel.EVENT_KIND_CODE),
        int_matrix(disabled, PrivateEquityIntChannel.EVENT_KIND_CODE),
    )
    np.testing.assert_array_equal(
        int_matrix(bare, PrivateEquityIntChannel.REGIME_CODE), int_matrix(disabled, PrivateEquityIntChannel.REGIME_CODE)
    )
    # Disabled channel emits the all-zeros sentinel.
    np.testing.assert_array_equal(
        matrix(disabled, PrivateEquityFloatChannel.COMPANY_VALUATION_USD),
        np.zeros((rollout_count, 19), dtype=np.float64),
    )


# ---- M2.2-A: per-rollout stochastic dilution rate ----------------------------------------

# A valuation-channel-ON issuer with a non-trivial dilution rate. The dispersion knob is
# overridden per test; the base leaves it at its inert default (0.0). Mark-overwriting events
# are suppressed (tenders pushed far beyond any test horizon with zero price noise; no other
# hazards) so the coupled mark equals current_mark * (V/V0) / dilution at every month -- letting
# tests compare the mark directly against the dilution formula.
_DILUTION_ISSUER_KWARGS = {
    "current_mark_usd": 100.0,
    "current_valuation_usd": 1_000_000_000.0,
    "shares_outstanding_initial": 10_000_000.0,
    "valuation_monthly_log_return_mu": 0.0,
    "valuation_monthly_log_return_sigma": 0.05,
    "valuation_student_t_nu": 5.0,
    "annual_dilution_rate": 0.20,
    "tender_interval_months_median": 100_000.0,
    "tender_price_log_discount_sigma": 0.0,
}


def _dilution_issuer(**overrides: object) -> PrivateEquityRiskIssuerConfig:
    return _issuer(**{**_DILUTION_ISSUER_KWARGS, **overrides})


def _sample_dilution_paths(issuer: PrivateEquityRiskIssuerConfig, *, rollout_count: int, horizon_months: int):
    """Run `_sample_issuer` directly over `rollout_count` rollouts (one seed each)."""

    request = ExogenousSamplingRequest(
        horizon_months=horizon_months,
        rollout_seeds=tuple(range(1, rollout_count + 1)),
        required_private_equity_issuers=frozenset({IssuerId("acme")}),
    )
    return _sample_issuer("acme", issuer, request)


def _drawn_rates(issuer: PrivateEquityRiskIssuerConfig, *, rollout_count: int) -> np.ndarray:
    """Reproduce the per-rollout dilution-rate draw the sampler performs."""

    seeds = tuple(range(1, rollout_count + 1))
    dilution_seeds = derive_stream_rollout_seeds(seeds, stream_id="acme:pe_risk_dilution")
    rng = np.random.default_rng(_seed_from_rollout_seeds(dilution_seeds))
    z = rng.standard_normal(rollout_count)
    return issuer.annual_dilution_rate * np.exp(issuer.annual_dilution_rate_log_sigma * z)


def _latent_coupled_mark(
    issuer: PrivateEquityRiskIssuerConfig, *, rollout_count: int, horizon_months: int
) -> np.ndarray:
    """Reconstruct the sampler's LATENT coupled mark `current_mark * (V(t)/V0) / dilution(t)`.

    The observed `_IssuerPaths.mark` channel is piecewise-constant between observation events
    (it only refreshes when a tender/admin/public-market/forced-sale event fires), so it does
    NOT continuously track the latent mark. These dilution tests probe the continuous coupling,
    so they reconstruct the latent mark from the same building blocks the sampler composes,
    using the same `:pe_risk_valuation` / `:pe_risk_dilution` seed streams.
    """

    assert issuer.current_valuation_usd is not None
    seeds = tuple(range(1, rollout_count + 1))
    valuation_seeds = derive_stream_rollout_seeds(seeds, stream_id="acme:pe_risk_valuation")
    company_valuation_usd = _sample_company_valuation_vectorized(
        issuer, valuation_seeds=valuation_seeds, horizon_months=horizon_months
    )
    dilution = _dilution_factor(
        annual_dilution_rate=issuer.annual_dilution_rate,
        annual_dilution_rate_log_sigma=issuer.annual_dilution_rate_log_sigma,
        rollout_seeds=seeds,
        issuer_id="acme",
        rollout_count=rollout_count,
        horizon_months=horizon_months,
    )
    return issuer.current_mark_usd * (company_valuation_usd / issuer.current_valuation_usd) / dilution


def test_sigma_zero_unified_path_is_byte_identical_to_deterministic_factor() -> None:
    """sigma=0 must degenerate through the UNIFIED per-rollout path with NO special-case.

    With the dispersion knob at its default (0.0) and the valuation channel on, the mark must
    equal `current_mark * (V/V0) / (1 + rate)^(t/12)` byte-for-byte, over many rollouts and a
    multi-year horizon with a non-trivial rate. This proves `exp(0) == 1` collapses the
    LogNormal naturally -- there is no `if sigma == 0` branch.
    """

    rollout_count = 64
    horizon = 120
    rate = 0.20
    issuer = _dilution_issuer(annual_dilution_rate=rate, annual_dilution_rate_log_sigma=0.0)
    paths = _sample_dilution_paths(issuer, rollout_count=rollout_count, horizon_months=horizon)
    latent_mark = _latent_coupled_mark(issuer, rollout_count=rollout_count, horizon_months=horizon)

    months = np.arange(horizon + 1, dtype=np.float64)
    deterministic_dilution = np.power(1.0 + rate, months / 12.0)[None, :]
    expected_mark = 100.0 * (paths.company_valuation_usd / 1_000_000_000.0) / deterministic_dilution
    np.testing.assert_array_equal(latent_mark, expected_mark)


def test_widening_cone_cross_rollout_variance_grows_in_time_and_with_sigma() -> None:
    """rate=0.20, sigma=0.3 -> cross-rollout var(log mark) at m120 > at m12, and > the sigma=0 case."""

    rollout_count = 400
    horizon = 120
    spread = _dilution_issuer(annual_dilution_rate=0.20, annual_dilution_rate_log_sigma=0.3)
    flat = _dilution_issuer(annual_dilution_rate=0.20, annual_dilution_rate_log_sigma=0.0)

    spread_mark = _latent_coupled_mark(spread, rollout_count=rollout_count, horizon_months=horizon)
    flat_mark = _latent_coupled_mark(flat, rollout_count=rollout_count, horizon_months=horizon)

    spread_log = np.log(spread_mark)
    var_m12 = float(np.var(spread_log[:, 12]))
    var_m120 = float(np.var(spread_log[:, 120]))
    # Quadratic-in-t widening cone from the per-rollout rate draw.
    assert var_m120 > var_m12
    # The sigma=0 case carries only V(t) spread; turning sigma on must add per-share spread at m120.
    assert var_m120 > float(np.var(np.log(flat_mark)[:, 120]))


def test_drawn_rate_median_is_anchored_at_annual_dilution_rate() -> None:
    """median(r) ~ annual_dilution_rate over many rollouts (median-anchored LogNormal)."""

    rate = 0.20
    issuer = _dilution_issuer(annual_dilution_rate=rate, annual_dilution_rate_log_sigma=0.4)
    rates = _drawn_rates(issuer, rollout_count=5000)
    # LogNormal median == exp(mu) == rate; the sample median converges to it (NOT the mean,
    # which would sit at rate * exp(sigma**2 / 2) ~ 0.217 here).
    assert float(np.median(rates)) == pytest.approx(rate, rel=0.05)


def test_positive_sigma_is_deterministic_under_fixed_seeds() -> None:
    issuer = _dilution_issuer(annual_dilution_rate=0.20, annual_dilution_rate_log_sigma=0.3)
    first = _sample_dilution_paths(issuer, rollout_count=32, horizon_months=60)
    second = _sample_dilution_paths(issuer, rollout_count=32, horizon_months=60)
    np.testing.assert_array_equal(first.mark, second.mark)


def test_dilution_sigma_does_not_perturb_event_or_regime_arrays() -> None:
    """Turning sigma from 0 to >0 leaves event_kind_code / regime_code byte-identical.

    The dilution draw is off the INDEPENDENT `:pe_risk_dilution` stream, so it must not touch
    the level/event RNG streams driving event timing and regime transitions. We use an issuer
    with live event hazards so the arrays are non-trivial.
    """

    rollout_count = 128
    horizon = 96
    base = {
        "annual_public_market_probability": 0.05,
        "annual_collapse_probability": 0.02,
        "annual_legal_event_probability": 0.03,
        "tender_interval_months_median": 12.0,
        "tender_interval_log_sigma": 0.4,
    }
    flat = _dilution_issuer(annual_dilution_rate=0.20, annual_dilution_rate_log_sigma=0.0, **base)
    spread = _dilution_issuer(annual_dilution_rate=0.20, annual_dilution_rate_log_sigma=0.5, **base)

    flat_paths = _sample_dilution_paths(flat, rollout_count=rollout_count, horizon_months=horizon)
    spread_paths = _sample_dilution_paths(spread, rollout_count=rollout_count, horizon_months=horizon)

    np.testing.assert_array_equal(flat_paths.event_kind_code, spread_paths.event_kind_code)
    np.testing.assert_array_equal(flat_paths.regime_code, spread_paths.regime_code)
    # Valuation channel is driven by its own stream too, so V(t) is unchanged.
    np.testing.assert_array_equal(flat_paths.company_valuation_usd, spread_paths.company_valuation_usd)


def test_zero_rate_with_positive_sigma_yields_no_dilution_and_no_spread() -> None:
    """rate=0, sigma>0 -> dilution factor all ones (0 * exp(sigma z) == 0), so no per-share spread."""

    rollout_count = 200
    horizon = 120
    factor = _dilution_factor(
        annual_dilution_rate=0.0,
        annual_dilution_rate_log_sigma=0.5,
        rollout_seeds=tuple(range(1, rollout_count + 1)),
        issuer_id="acme",
        rollout_count=rollout_count,
        horizon_months=horizon,
    )
    np.testing.assert_array_equal(factor, np.ones((rollout_count, horizon + 1)))

    # End-to-end: with rate 0 the coupled mark is exactly current_mark * V(t)/V0 regardless of
    # sigma -- the dilution channel contributes zero spread.
    issuer = _dilution_issuer(annual_dilution_rate=0.0, annual_dilution_rate_log_sigma=0.5)
    paths = _sample_dilution_paths(issuer, rollout_count=rollout_count, horizon_months=horizon)
    latent_mark = _latent_coupled_mark(issuer, rollout_count=rollout_count, horizon_months=horizon)
    expected = 100.0 * (paths.company_valuation_usd / 1_000_000_000.0)
    np.testing.assert_array_equal(latent_mark, expected)


if __name__ == "__main__":
    pytest_bazel.main()
