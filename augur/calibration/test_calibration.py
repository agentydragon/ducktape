"""End-to-end `run_calibration` against a tiny synthetic model (no network)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent

import numpy as np
import polars as pl
import pytest_bazel

from augur.calibration.calibration import (
    CalibrationResult,
    mark_fan,
    run_calibration,
    sample_private_equity_bundle,
    wilson_interval,
)
from augur.calibration.catalog import MarketCatalog
from augur.model.exogenous import SERIES_LEVELS_SCHEMA, ExogenousSamplingRequest, SampledExogenousBundle
from augur.model.private_equity_bundle import PrivateEquityBundle, PrivateEquityFloatChannel
from augur.model.series import IssuerId, PrivateEquityEventKindCode, PrivateEquityRegimeCode

_ISSUER = "issuer_x"
_HORIZON = 120

# Catalog with one exact ipo_by_date market, one exact pre_ipo_failure market, and a
# surfaced correlate market whose `correlate_of: ipo_by_date` triggers augur_context.
_CATALOG = dedent(
    """
    metadata:
      as_of: "2026-05-29"
      augur_model_as_of: "2026-05-27"
    markets:
      - slug: ipo-before-2027
        manifold_id: AAA
        question: "Issuer IPO before 2027?"
        outcome_type: BINARY
        resolution_deadline: "2027-01-01"
        mappability: exact
        mapping_kind: ipo_by_date
        mapping_params: {by_date: "2027-01-01"}
        curation_snapshot: {yes_prob: 0.40}
      - slug: collapse-before-ipo
        manifold_id: BBB
        question: "Issuer collapses or acquired before IPO?"
        outcome_type: BINARY
        resolution_deadline: null
        mappability: exact
        mapping_kind: pre_ipo_failure
        mapping_params: {before_event: PUBLIC_MARKET_OPEN}
        curation_snapshot: {yes_prob: 0.10}
      - slug: valuation-1t
        manifold_id: CCC
        question: "Issuer completes an IPO in 2026 with $1T cap?"
        outcome_type: BINARY
        resolution_deadline: "2026-12-31"
        mappability: correlate
        correlate_of: ipo_by_date
        correlate_strength: strong
        reason: >
          The >=$1T cap conjunct needs a valuation augur does not model.
        curation_snapshot: {yes_prob: 0.66}
    """
)


@dataclass(frozen=True)
class _CraftedSampler:
    """Returns a fixed PE bundle: 4 rollouts (IPO@7, IPO@60, collapse@3, private)."""

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        rollout_count, months = request.rollout_count, request.horizon_months + 1
        marks = np.full((rollout_count, months), 50.0, dtype=np.float64)
        events = np.full((rollout_count, months), int(PrivateEquityEventKindCode.NONE), dtype=np.int64)
        regimes = np.full((rollout_count, months), int(PrivateEquityRegimeCode.PRIVATE_OPERATING), dtype=np.int64)
        # rollout 0: IPO at month 7 (before the 2027 deadline)
        events[0, 7] = int(PrivateEquityEventKindCode.PUBLIC_MARKET_OPEN)
        regimes[0, 7:] = int(PrivateEquityRegimeCode.PUBLIC_MARKET)
        # rollout 1: IPO at month 60 (after the 2027 deadline, inside horizon)
        events[1, 60] = int(PrivateEquityEventKindCode.PUBLIC_MARKET_OPEN)
        regimes[1, 60:] = int(PrivateEquityRegimeCode.PUBLIC_MARKET)
        # rollout 2: collapse at month 3 (pre-IPO failure)
        events[2, 3] = int(PrivateEquityEventKindCode.COLLAPSE)
        regimes[2, 3:] = int(PrivateEquityRegimeCode.COLLAPSED)
        # rollout 3: stays private the whole horizon
        zeros = np.zeros((rollout_count, months), dtype=np.float64)
        bundle = PrivateEquityBundle.from_issuer_arrays(
            IssuerId(_ISSUER),
            mark_usd_per_unit=marks,
            regime_code=regimes,
            event_kind_code=events,
            sale_opportunity_active=np.zeros((rollout_count, months), dtype=np.bool_),
            sale_capacity_fraction=np.ones((rollout_count, months), dtype=np.float64),
            eligible_fraction=np.ones((rollout_count, months), dtype=np.float64),
            forced_sale_fraction=zeros,
            liquidity_blocked=np.zeros((rollout_count, months), dtype=np.bool_),
            forced_recovery_cashout_usd=zeros,
            rollout_count=rollout_count,
            horizon_months=request.horizon_months,
        )
        return SampledExogenousBundle(levels=pl.DataFrame(schema=SERIES_LEVELS_SCHEMA), private_equity=bundle)


def _result(tmp_path: Path) -> CalibrationResult:
    path = tmp_path / "catalog.yaml"
    path.write_text(_CATALOG, encoding="utf-8")
    return run_calibration(
        _CraftedSampler(),
        MarketCatalog.from_yaml(path),
        issuer=_ISSUER,
        horizon_months=_HORIZON,
        rollout_seeds=tuple(range(4)),
    )


def test_clean_rows_score_events(tmp_path: Path) -> None:
    result = _result(tmp_path)
    assert result.issuer == _ISSUER
    assert result.rollout_count == 4
    assert result.price_source == "curation-snapshot"
    clean = {row.slug: row for row in result.clean}

    # ipo_by_date(2027-01-01 -> month 7): rollout 0 YES; rollouts 1,3 NO (no IPO by then,
    # whole horizon simulated); rollout 2 NO (collapsed, no IPO). All resolved.
    ipo = clean["ipo-before-2027"]
    assert ipo.n_resolved == 4
    assert ipo.unresolved == 0
    assert ipo.p_model == 0.25
    assert ipo.p_market == 0.40
    assert ipo.abs_gap is not None
    assert math.isclose(ipo.abs_gap, 0.15)

    # pre_ipo_failure: rollout 2 YES (collapse before IPO); rollouts 0,1 NO (IPO first);
    # rollout 3 UNRESOLVED (still private at horizon end).
    fail = clean["collapse-before-ipo"]
    assert fail.n_resolved == 3
    assert fail.unresolved == 1
    assert fail.p_model is not None
    assert math.isclose(fail.p_model, 1 / 3)


def test_surfaced_row_carries_augur_context(tmp_path: Path) -> None:
    result = _result(tmp_path)
    assert [row.slug for row in result.surfaced] == ["valuation-1t"]
    surfaced = result.surfaced[0]
    assert surfaced.correlate_of == "ipo_by_date"
    assert surfaced.reason == "The >=$1T cap conjunct needs a valuation augur does not model."
    # deadline 2026-12-31 is month 7 from as_of 2026-05-27; rollout 0 IPOs at month 7 (<=7),
    # the others don't -> P(IPO by deadline) = 1/4. This is surfaced context, NOT a score.
    assert surfaced.augur_context is not None
    assert surfaced.augur_context.signal == "P(PUBLIC_MARKET_OPEN by deadline)"
    assert surfaced.augur_context.p_model == 0.25


def test_result_is_json_serializable(tmp_path: Path) -> None:
    payload = _result(tmp_path).model_dump_json()
    assert '"ipo-before-2027"' in payload


def test_mark_fan_shape() -> None:
    request = ExogenousSamplingRequest(horizon_months=_HORIZON, rollout_seeds=tuple(range(4)))
    bundle = _CraftedSampler().sample(request).private_equity
    fan = mark_fan(bundle, issuer=_ISSUER, rollout_count=4, horizon_months=_HORIZON, percentiles=(5.0, 50.0, 95.0))
    assert fan.channel == PrivateEquityFloatChannel.MARK_USD_PER_UNIT
    assert fan.percentiles == [5.0, 50.0, 95.0]
    assert len(fan.months) == _HORIZON + 1
    # Constant 50.0 mark -> every percentile band is 50.0.
    assert fan.months[0].values == {"5.0": 50.0, "50.0": 50.0, "95.0": 50.0}
    # Round-trips to JSON for a backend.
    assert '"50.0"' in fan.model_dump_json()


def test_run_calibration_reuses_supplied_bundle(tmp_path: Path) -> None:
    # Sampling the bundle once and threading it into run_calibration must yield the same
    # scoring as letting run_calibration sample internally — the backend reuses one bundle
    # for both the clean/surfaced scoring and the mark_fan.
    path = tmp_path / "catalog.yaml"
    path.write_text(_CATALOG, encoding="utf-8")
    catalog = MarketCatalog.from_yaml(path)
    seeds = tuple(range(4))
    bundle = sample_private_equity_bundle(
        _CraftedSampler(), issuer=_ISSUER, horizon_months=_HORIZON, rollout_seeds=seeds
    )
    from_bundle = run_calibration(
        _CraftedSampler(), catalog, issuer=_ISSUER, horizon_months=_HORIZON, rollout_seeds=seeds, bundle=bundle
    )
    internal = run_calibration(_CraftedSampler(), catalog, issuer=_ISSUER, horizon_months=_HORIZON, rollout_seeds=seeds)
    assert from_bundle == internal
    # The same bundle also drives the mark_fan, so both views come from one rollout.
    fan = mark_fan(bundle, issuer=_ISSUER, rollout_count=4, horizon_months=_HORIZON, percentiles=(50.0,))
    assert fan.months[0].values == {"50.0": 50.0}


def test_wilson_interval_edges() -> None:
    assert all(math.isnan(x) for x in wilson_interval(0, 0))
    lo, hi = wilson_interval(5, 10)
    # p_hat = 0.5 -> the 95% Wilson interval is symmetric about 0.5 and strictly inside (0, 1).
    assert 0.0 < lo < 0.5 < hi < 1.0
    assert math.isclose((lo + hi) / 2, 0.5, abs_tol=1e-9)


if __name__ == "__main__":
    pytest_bazel.main()
