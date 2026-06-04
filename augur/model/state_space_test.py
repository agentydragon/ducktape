from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
import pytest_bazel

from augur.model.conditioning import ExogenousConditioningContext, ExogenousObservedPoint, ObservationTreatment
from augur.model.exogenous import ExogenousSamplingRequest, level_keys_in_bundle, level_series_request_channels
from augur.model.series import (
    CryptoKey,
    CryptoSymbol,
    HomeValueKey,
    InflationKey,
    IssuerId,
    LocationId,
    RentKey,
    SP500Key,
)
from augur.model.state_space import (
    StateSpaceModel,
    StateSpaceModelArtifact,
    StateSpacePrivateEquityEventPrior,
    StateSpaceProviderConfig,
    write_state_space_artifact,
)
from augur.model.trained_private_equity import TrainedPrivateEquityScalePrior
from augur.product.asset_key import PrivateEquityAssetKey


def test_state_space_samples_all_available_series_and_hard_anchors(tmp_path: Path) -> None:
    provider = _provider(tmp_path, sp500_anchor=123.0)
    sampled = provider.realize_model().sample(
        ExogenousSamplingRequest(
            rollout_seeds=(7, 8), horizon_months=3, **level_series_request_channels(frozenset({InflationKey()}))
        )
    )

    level_keys = level_keys_in_bundle(sampled)
    # Post-collapse, every level factor is its own key; there is no aliased extra location
    # (the artifact has a single home_value:san_francisco_ca factor, not a mare-island alias).
    assert level_keys >= {
        SP500Key(),
        InflationKey(),
        CryptoKey(symbol=CryptoSymbol("btc")),
        HomeValueKey(location_id=LocationId("san_francisco_ca")),
        RentKey(location_id=LocationId("san_francisco_ca")),
    }
    assert sampled.private_equity.issuer_ids() >= frozenset({"private_company_a"})
    np.testing.assert_allclose(
        sampled.level_matrix(SP500Key(), rollout_count=2, horizon_months=3)[:, 0], np.array([123.0, 123.0])
    )
    assert sampled.private_equity.issuer_bool_matrix(
        "private_company_a", "sale_opportunity_active", rollout_count=2, horizon_months=3
    ).shape == (2, 4)
    source_manifest = cast(dict[str, Any], sampled.metadata["source_manifest"])
    prior_manifest = cast(dict[str, Any], sampled.metadata["prior_manifest"])
    assert source_manifest["source_ids"] == ["fixture:public"]
    assert prior_manifest["kind"] == "fixture"


def test_state_space_conditioning_changes_sampled_paths(tmp_path: Path) -> None:
    low = (
        _provider(tmp_path / "low", sp500_anchor=100.0)
        .realize_model()
        .sample(ExogenousSamplingRequest(rollout_seeds=(11,), horizon_months=3))
    )
    high = (
        _provider(tmp_path / "high", sp500_anchor=200.0)
        .realize_model()
        .sample(ExogenousSamplingRequest(rollout_seeds=(11,), horizon_months=3))
    )

    low_sp500 = low.level_matrix(SP500Key(), rollout_count=1, horizon_months=3)
    high_sp500 = high.level_matrix(SP500Key(), rollout_count=1, horizon_months=3)
    np.testing.assert_allclose(high_sp500, low_sp500 * 2.0)


def test_state_space_private_equity_marks_forward_fill_between_tenders(tmp_path: Path) -> None:
    sampled = (
        _provider(
            tmp_path, sp500_anchor=100.0, pe_tender_interval_months_median=120.0, pe_tender_interval_log_sigma=1e-12
        )
        .realize_model()
        .sample(
            ExogenousSamplingRequest(
                rollout_seeds=(11,),
                horizon_months=4,
                required_private_equity_issuers=frozenset({IssuerId("private_company_a")}),
            )
        )
    )

    levels = sampled.private_equity.issuer_float_matrix(
        "private_company_a", "mark_usd_per_unit", rollout_count=1, horizon_months=4
    )
    np.testing.assert_allclose(levels, np.full((1, 5), 687.69))


def test_state_space_hard_fails_missing_required_series(tmp_path: Path) -> None:
    model = _provider(tmp_path, sp500_anchor=123.0).realize_model()

    # `prices_of_tea` isn't a level series the artifact models; rent in an
    # unmodeled location is the closest typed-key equivalent of "unknown".
    with pytest.raises(ValueError, match="missing required level series"):
        model.sample(
            ExogenousSamplingRequest(
                rollout_seeds=(1,),
                horizon_months=1,
                **level_series_request_channels(frozenset({RentKey(location_id=LocationId("nowhere_xx"))})),
            )
        )


def _provider(
    path: Path,
    *,
    sp500_anchor: float,
    pe_tender_interval_months_median: float = 2.0,
    pe_tender_interval_log_sigma: float = 0.1,
) -> StateSpaceProviderConfig:
    path.mkdir(parents=True, exist_ok=True)
    artifact_path = path / "state_space.json"
    write_state_space_artifact(
        artifact_path,
        _artifact(
            pe_tender_interval_months_median=pe_tender_interval_months_median,
            pe_tender_interval_log_sigma=pe_tender_interval_log_sigma,
        ),
    )
    conditioning = ExogenousConditioningContext(
        start_at=date(2026, 5, 1),
        observations={
            SP500Key().wire_id: (
                ExogenousObservedPoint(
                    value=sp500_anchor,
                    observed_at=date(2026, 5, 1),
                    source_id="fixture:sp500",
                    treatment=ObservationTreatment.HARD_START,
                ),
            )
        },
    )
    return StateSpaceProviderConfig(
        trained_artifact_path=artifact_path, conditioning=conditioning, current_mortgage30_rate_pct=6.25
    )


def _artifact(
    *,
    pe_tender_interval_months_median: float = 2.0,
    pe_tender_interval_log_sigma: float = 0.1,
    emitted_factor_copies: dict[str, str] | None = None,
) -> StateSpaceModelArtifact:
    sp500 = SP500Key().wire_id
    inflation = InflationKey().wire_id
    btc = CryptoKey(symbol=CryptoSymbol("btc")).wire_id
    hv_sf = HomeValueKey(location_id=LocationId("san_francisco_ca")).wire_id
    rent_sf = RentKey(location_id=LocationId("san_francisco_ca")).wire_id
    pe = PrivateEquityAssetKey(issuer_id=IssuerId("private_company_a")).wire_id
    factors = (sp500, inflation, btc, hv_sf, rent_sf, pe)
    latest = {sp500: 100.0, inflation: 320.0, btc: 80_000.0, hv_sf: 1_400_000.0, rent_sf: 530.0, pe: 687.69}
    mu = {sp500: 0.005, inflation: 0.002, btc: 0.01, hv_sf: 0.003, rent_sf: 0.0025, pe: 0.01}
    cov = np.diag([0.04**2, 0.003**2, 0.2**2, 0.01**2, 0.006**2, 0.08**2])
    return StateSpaceModelArtifact(
        factor_names=factors,
        trained_through_month="2026-04",
        latest_level_by_factor=latest,
        monthly_log_return_mu=mu,
        monthly_log_return_cov=tuple(tuple(float(value) for value in row) for row in cov),
        filtered_log_state_mean={factor: float(np.log(latest[factor])) for factor in factors},
        filtered_log_state_cov=tuple(tuple(float(value) for value in row) for row in cov),
        private_equity_event_priors={
            "private_company_a": StateSpacePrivateEquityEventPrior(
                tender_interval_months_median=pe_tender_interval_months_median,
                tender_interval_log_sigma=pe_tender_interval_log_sigma,
                last_tender_observed_at=date(2026, 1, 1),
            )
        },
        private_equity_scale_priors={
            "private_company_a": TrainedPrivateEquityScalePrior(
                current_market_cap_usd=7_000_000_000.0,
                soft_cap_market_cap_usd=5_000_000_000_000.0,
                monthly_log_drift_penalty=0.08,
            )
        },
        source_manifest={"source_ids": ("fixture:public",)},
        prior_manifest={"kind": "fixture"},
        emitted_factor_copies=emitted_factor_copies or {},
    )


def test_state_space_emits_chosen_factor_copies_with_identical_draws() -> None:
    # The model privately chooses to emit a second location's home_value/rent equal to a fitted
    # location's draws; the copy series must be emittable and numerically identical to its source.
    hv_sf = HomeValueKey(location_id=LocationId("san_francisco_ca"))
    rent_sf = RentKey(location_id=LocationId("san_francisco_ca"))
    hv_mi = HomeValueKey(location_id=LocationId("mare_island_vallejo_ca"))
    rent_mi = RentKey(location_id=LocationId("mare_island_vallejo_ca"))
    artifact = _artifact(emitted_factor_copies={hv_mi.wire_id: hv_sf.wire_id, rent_mi.wire_id: rent_sf.wire_id})
    model = StateSpaceModel(artifact=artifact, conditioning=ExogenousConditioningContext(start_at=date(2026, 5, 1)))

    assert {hv_mi, rent_mi} <= model.emittable_level_keys()
    sampled = model.sample(
        ExogenousSamplingRequest(
            rollout_seeds=(3, 4), horizon_months=3, **level_series_request_channels(frozenset({hv_mi, rent_mi}))
        )
    )
    for copy_key, source_key in ((hv_mi, hv_sf), (rent_mi, rent_sf)):
        np.testing.assert_array_equal(
            sampled.level_matrix(copy_key, rollout_count=2, horizon_months=3),
            sampled.level_matrix(source_key, rollout_count=2, horizon_months=3),
        )


def test_state_space_artifact_rejects_invalid_factor_copies() -> None:
    hv_sf = HomeValueKey(location_id=LocationId("san_francisco_ca")).wire_id
    rent_sf = RentKey(location_id=LocationId("san_francisco_ca")).wire_id
    hv_mi = HomeValueKey(location_id=LocationId("mare_island_vallejo_ca")).wire_id
    with pytest.raises(ValueError, match="not a fitted factor"):
        _artifact(emitted_factor_copies={hv_mi: HomeValueKey(location_id=LocationId("nowhere_xx")).wire_id})
    with pytest.raises(ValueError, match="already a fitted factor"):
        _artifact(emitted_factor_copies={hv_sf: hv_sf})
    with pytest.raises(ValueError, match="must share kind"):
        _artifact(emitted_factor_copies={hv_mi: rent_sf})


if __name__ == "__main__":
    pytest_bazel.main()
