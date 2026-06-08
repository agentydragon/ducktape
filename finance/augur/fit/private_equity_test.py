from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import pytest_bazel

from finance.augur.fit.private_equity import (
    PrivateEquityTrainingConfig,
    PrivateEquityTrainingPriors,
    fit_private_equity_model,
    load_price_observations_jsonl,
    train_from_config,
)
from finance.augur.model.exogenous import ExogenousSamplingRequest
from finance.augur.model.series import IssuerId
from finance.augur.model.trained_private_equity import (
    TrainedPrivateEquityModel,
    TrainedPrivateEquityModelArtifact,
    TrainedPrivateEquityScalePrior,
)
from util.testing.jsonl import write_jsonl


@pytest.fixture
def rows() -> list[dict[str, object]]:
    return [
        {
            "type": "price_observation",
            "issuer_id": "private_company_a",
            "observed_at": "2023-11-15",
            "kind": "tender_price",
            "price_usd_per_share": 150.0,
            "uncertainty_log_sigma": 0.08,
            "source_id": "test",
            "notes": "synthetic tender",
        },
        {
            "type": "price_observation",
            "issuer_id": "private_company_a",
            "observed_at": "2024-11-15",
            "kind": "tender_price",
            "price_usd_per_share": 210.0,
            "uncertainty_log_sigma": 0.08,
            "source_id": "test",
            "notes": "synthetic tender",
        },
        {
            "type": "valuation_observation",
            "issuer_id": "private_company_a",
            "observed_at": "2024-11-15",
            "valuation_usd": 2_100_000_000.0,
            "uncertainty_log_sigma": 0.15,
            "valuation_kind": "implied",
            "source_id": "test",
            "notes": "synthetic valuation paired with tender price",
        },
        {
            "type": "price_observation",
            "issuer_id": "private_company_a",
            "observed_at": "2026-05-27",
            "kind": "ppu_mark",
            "price_usd_per_share": 687.69,
            "uncertainty_log_sigma": 0.10,
            "source_id": "fixture_current_mark",
            "notes": "synthetic current mark",
        },
    ]


@pytest.fixture
def broad_scale_prior() -> TrainedPrivateEquityScalePrior:
    return TrainedPrivateEquityScalePrior(
        current_market_cap_usd=10_000_000_000.0,
        soft_cap_market_cap_usd=1_000_000_000_000.0,
        monthly_log_drift_penalty=0.20,
    )


def test_load_jsonl_accepts_valuation_observations(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "observations.jsonl",
        [
            {
                "type": "valuation_observation",
                "issuer_id": "private_company_a",
                "observed_at": "2025-10-28",
                "valuation_usd": 500_000_000_000,
                "uncertainty_log_sigma": 0.2,
                "valuation_kind": "implied",
                "source_id": "test",
            }
        ],
    )

    observations = load_price_observations_jsonl(path)

    assert len(observations) == 1
    assert observations[0].type == "valuation_observation"


def test_primary_valuation_requires_cash_raised(tmp_path: Path) -> None:
    """`valuation_kind=primary` without `cash_raised_usd` is rejected at parse time."""
    path = write_jsonl(
        tmp_path / "observations.jsonl",
        [
            {
                "type": "valuation_observation",
                "issuer_id": "private_company_a",
                "observed_at": "2025-03-15",
                "valuation_usd": 300_000_000_000.0,
                "uncertainty_log_sigma": 0.05,
                "valuation_kind": "primary",
                "source_id": "test",
            }
        ],
    )
    with pytest.raises(ValueError, match="primary valuation_observation requires cash_raised_usd"):
        load_price_observations_jsonl(path)


def test_non_primary_valuation_rejects_cash_raised(tmp_path: Path) -> None:
    """`cash_raised_usd` set on a non-primary observation is rejected (catches mis-tags)."""
    path = write_jsonl(
        tmp_path / "observations.jsonl",
        [
            {
                "type": "valuation_observation",
                "issuer_id": "private_company_a",
                "observed_at": "2025-10-15",
                "valuation_usd": 500_000_000_000.0,
                "uncertainty_log_sigma": 0.10,
                "valuation_kind": "secondary",
                "cash_raised_usd": 6_600_000_000.0,
                "source_id": "test",
            }
        ],
    )
    with pytest.raises(ValueError, match="cash_raised_usd is only valid when valuation_kind='primary'"):
        load_price_observations_jsonl(path)


def test_primary_valuation_accepts_cash_raised(tmp_path: Path) -> None:
    """Happy path: primary kind + cash_raised_usd + optional shares_outstanding_post_round."""
    path = write_jsonl(
        tmp_path / "observations.jsonl",
        [
            {
                "type": "valuation_observation",
                "issuer_id": "private_company_a",
                "observed_at": "2025-03-15",
                "valuation_usd": 300_000_000_000.0,
                "uncertainty_log_sigma": 0.05,
                "valuation_kind": "primary",
                "cash_raised_usd": 40_000_000_000.0,
                "shares_outstanding_post_round": 1_000_000_000.0,
                "source_id": "test",
            }
        ],
    )
    observations = load_price_observations_jsonl(path)
    assert len(observations) == 1
    valuation = observations[0]
    assert valuation.type == "valuation_observation"
    assert valuation.valuation_kind == "primary"
    assert valuation.cash_raised_usd == 40_000_000_000.0
    assert valuation.shares_outstanding_post_round == 1_000_000_000.0


def test_load_jsonl_rejects_unknown_observation_type(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "observations.jsonl",
        [{"type": "mystery_observation", "issuer_id": "private_company_a", "observed_at": "2025-10-28"}],
    )

    with pytest.raises(ValueError, match="unsupported observation type"):
        load_price_observations_jsonl(path)


def test_fit_requires_current_ppu_mark(tmp_path: Path, rows: list[dict[str, object]]) -> None:
    observations = load_price_observations_jsonl(write_jsonl(tmp_path / "observations.jsonl", rows[:2]))
    config = PrivateEquityTrainingConfig(
        issuer_id="private_company_a",
        observations_path="observations.jsonl",
        out_model_path="model.json",
        priors=PrivateEquityTrainingPriors(macro_capacity_reference_usd=100_000_000_000.0),
    )

    with pytest.raises(ValueError, match="ppu_mark"):
        fit_private_equity_model(observations, config)


def test_train_round_trips_compact_model_and_runtime_samples(tmp_path: Path, rows: list[dict[str, object]]) -> None:
    write_jsonl(tmp_path / "observations.jsonl", rows)
    config_path = tmp_path / "train.yaml"
    config_path.write_text(
        """
issuer_id: private_company_a
observations_path: observations.jsonl
out_model_path: trained_model.json
priors:
  macro_capacity_reference_usd: 100000000000.0
  tender_interval_months_median_prior: 3.0
  tender_interval_log_sigma: 0.05
  tender_price_log_discount_sigma: 0.0
""".strip()
        + "\n",
        encoding="utf-8",
    )

    artifact = train_from_config(config_path)
    assert artifact.issuer_id == "private_company_a"
    assert artifact.current_mark_usd == 687.69
    assert (tmp_path / "trained_model.json").exists()

    model = TrainedPrivateEquityModel.from_path(tmp_path / "trained_model.json")
    request = ExogenousSamplingRequest(
        horizon_months=8,
        rollout_seeds=(1, 2, 3),
        required_private_equity_issuers=frozenset({IssuerId("private_company_a")}),
    )
    bundle = model.sample(request)

    levels = bundle.private_equity.issuer_float_matrix(
        "private_company_a", "mark_usd_per_unit", rollout_count=3, horizon_months=8
    )
    assert levels.shape == (3, 9)
    np.testing.assert_allclose(levels[:, 0], np.array([687.69, 687.69, 687.69]))
    assert (levels > 0).all()
    events = bundle.private_equity.issuer_bool_matrix(
        "private_company_a", "sale_opportunity_active", rollout_count=3, horizon_months=8
    )
    assert events.dtype.kind == "b"
    assert events.shape == (3, 9)


def test_runtime_private_marks_forward_fill_between_tenders(broad_scale_prior: TrainedPrivateEquityScalePrior) -> None:
    model = TrainedPrivateEquityModel(
        artifact=TrainedPrivateEquityModelArtifact(
            issuer_id="private_company_a",
            as_of_date="2026-05-27",
            current_mark_usd=100.0,
            monthly_log_return_mu=float(np.log(2.0)),
            monthly_log_return_sigma=1e-9,
            tender_interval_months_median=120.0,
            tender_interval_log_sigma=1e-12,
            tender_price_log_discount_sigma=0.0,
            scale_prior=broad_scale_prior,
        )
    )

    sampled = model.sample(
        ExogenousSamplingRequest(
            horizon_months=4,
            rollout_seeds=(1,),
            required_private_equity_issuers=frozenset({IssuerId("private_company_a")}),
        )
    )
    levels = sampled.private_equity.issuer_float_matrix(
        "private_company_a", "mark_usd_per_unit", rollout_count=1, horizon_months=4
    )

    np.testing.assert_allclose(levels, np.full((1, 5), 100.0))


def test_runtime_tender_updates_observed_private_mark(broad_scale_prior: TrainedPrivateEquityScalePrior) -> None:
    model = TrainedPrivateEquityModel(
        artifact=TrainedPrivateEquityModelArtifact(
            issuer_id="private_company_a",
            as_of_date="2026-05-27",
            current_mark_usd=100.0,
            monthly_log_return_mu=float(np.log(2.0) / 2.0),
            monthly_log_return_sigma=1e-12,
            tender_interval_months_median=2.0,
            tender_interval_log_sigma=1e-12,
            tender_price_log_discount_sigma=0.0,
            scale_prior=broad_scale_prior,
        )
    )

    sampled = model.sample(
        ExogenousSamplingRequest(
            horizon_months=4,
            rollout_seeds=(1,),
            required_private_equity_issuers=frozenset({IssuerId("private_company_a")}),
        )
    )
    levels = sampled.private_equity.issuer_float_matrix(
        "private_company_a", "mark_usd_per_unit", rollout_count=1, horizon_months=4
    )
    events = sampled.private_equity.issuer_bool_matrix(
        "private_company_a", "sale_opportunity_active", rollout_count=1, horizon_months=4
    )

    assert events[0, 2]
    assert events[0, 4]
    assert levels[0, 0] == pytest.approx(100.0)
    assert levels[0, 1] == pytest.approx(100.0)
    assert levels[0, 2] == pytest.approx(200.0)
    assert levels[0, 3] == pytest.approx(200.0)
    assert levels[0, 4] == pytest.approx(400.0)


def test_sparse_tender_appreciation_is_shrunk_toward_stock_like_forward_prior(tmp_path: Path) -> None:
    observations = load_price_observations_jsonl(
        write_jsonl(
            tmp_path / "observations.jsonl",
            [
                {
                    "type": "price_observation",
                    "issuer_id": "private_company_a",
                    "observed_at": "2025-01-01",
                    "kind": "tender_price",
                    "price_usd_per_share": 10.0,
                    "uncertainty_log_sigma": 0.05,
                    "source_id": "test",
                },
                {
                    "type": "price_observation",
                    "issuer_id": "private_company_a",
                    "observed_at": "2025-07-01",
                    "kind": "ppu_mark",
                    "price_usd_per_share": 100.0,
                    "uncertainty_log_sigma": 0.05,
                    "source_id": "test",
                },
                {
                    "type": "valuation_observation",
                    "issuer_id": "private_company_a",
                    "observed_at": "2025-07-01",
                    "valuation_usd": 1_000_000_000.0,
                    "uncertainty_log_sigma": 0.10,
                    "valuation_kind": "implied",
                    "source_id": "test",
                },
            ],
        )
    )
    config = PrivateEquityTrainingConfig(
        issuer_id="private_company_a",
        observations_path="observations.jsonl",
        out_model_path="model.json",
        priors=PrivateEquityTrainingPriors(
            macro_capacity_reference_usd=100_000_000_000.0,
            stock_like_monthly_log_return_mu=0.005,
            stock_like_monthly_log_return_mu_weight_months=240.0,
            stock_like_monthly_log_return_sigma=0.10,
            stock_like_monthly_log_return_sigma_weight_returns=60.0,
        ),
    )

    artifact = fit_private_equity_model(observations, config)

    assert artifact.provenance["empirical_monthly_log_return_mu"] > 0.35
    assert artifact.monthly_log_return_mu < 0.02
    assert artifact.monthly_log_return_sigma < 0.12


def test_valuation_observations_create_soft_macro_scale_prior(tmp_path: Path) -> None:
    observations = load_price_observations_jsonl(
        write_jsonl(
            tmp_path / "observations.jsonl",
            [
                {
                    "type": "price_observation",
                    "issuer_id": "private_company_a",
                    "observed_at": "2025-01-01",
                    "kind": "tender_price",
                    "price_usd_per_share": 100.0,
                    "uncertainty_log_sigma": 0.05,
                    "source_id": "test",
                },
                {
                    "type": "valuation_observation",
                    "issuer_id": "private_company_a",
                    "observed_at": "2025-01-15",
                    "valuation_usd": 1_000_000_000.0,
                    "uncertainty_log_sigma": 0.10,
                    "valuation_kind": "implied",
                    "source_id": "test",
                },
                {
                    "type": "price_observation",
                    "issuer_id": "private_company_a",
                    "observed_at": "2026-01-01",
                    "kind": "ppu_mark",
                    "price_usd_per_share": 150.0,
                    "uncertainty_log_sigma": 0.05,
                    "source_id": "test",
                },
            ],
        )
    )
    config = PrivateEquityTrainingConfig(
        issuer_id="private_company_a",
        observations_path="observations.jsonl",
        out_model_path="model.json",
        priors=PrivateEquityTrainingPriors(
            macro_capacity_reference_usd=100_000_000_000.0,
            macro_capacity_soft_fraction=0.05,
            macro_capacity_monthly_log_drift_penalty=0.10,
        ),
    )

    artifact = fit_private_equity_model(observations, config)

    assert artifact.scale_prior.current_market_cap_usd == pytest.approx(1_500_000_000.0)
    assert artifact.scale_prior.soft_cap_market_cap_usd == pytest.approx(5_000_000_000.0)


def test_runtime_scale_prior_penalizes_paths_above_soft_cap() -> None:
    common = {
        "issuer_id": "private_company_a",
        "as_of_date": "2026-05-27",
        "current_mark_usd": 100.0,
        "monthly_log_return_mu": 0.20,
        "monthly_log_return_sigma": 0.0 + 1e-9,
        "tender_interval_months_median": 1.0,
        "tender_interval_log_sigma": 1e-12,
        "tender_price_log_discount_sigma": 0.0,
    }
    loose = TrainedPrivateEquityModel(
        artifact=TrainedPrivateEquityModelArtifact(
            **common,
            scale_prior=TrainedPrivateEquityScalePrior(
                current_market_cap_usd=10_000_000_000.0,
                soft_cap_market_cap_usd=1_000_000_000_000.0,
                monthly_log_drift_penalty=0.20,
            ),
        )
    )
    tight = TrainedPrivateEquityModel(
        artifact=TrainedPrivateEquityModelArtifact(
            **common,
            scale_prior=TrainedPrivateEquityScalePrior(
                current_market_cap_usd=10_000_000_000.0,
                soft_cap_market_cap_usd=1_000_000_000.0,
                monthly_log_drift_penalty=0.20,
            ),
        )
    )
    request = ExogenousSamplingRequest(
        horizon_months=12,
        rollout_seeds=(1,),
        required_private_equity_issuers=frozenset({IssuerId("private_company_a")}),
    )

    loose_levels = loose.sample(request).private_equity.issuer_float_matrix(
        "private_company_a", "mark_usd_per_unit", rollout_count=1, horizon_months=12
    )
    tight_levels = tight.sample(request).private_equity.issuer_float_matrix(
        "private_company_a", "mark_usd_per_unit", rollout_count=1, horizon_months=12
    )

    assert tight_levels[0, -1] < loose_levels[0, -1]


def test_runtime_sampling_fails_on_nonfinite_private_equity_prices(
    broad_scale_prior: TrainedPrivateEquityScalePrior,
) -> None:
    model = TrainedPrivateEquityModel(
        artifact=TrainedPrivateEquityModelArtifact(
            issuer_id="private_company_a",
            as_of_date="2026-05-27",
            current_mark_usd=687.69,
            monthly_log_return_mu=1000.0,
            monthly_log_return_sigma=0.01,
            tender_interval_months_median=12.0,
            tender_interval_log_sigma=0.1,
            scale_prior=broad_scale_prior,
        )
    )

    with pytest.raises(ValueError, match="non-finite prices"):
        model.sample(
            ExogenousSamplingRequest(
                horizon_months=1,
                rollout_seeds=(1,),
                required_private_equity_issuers=frozenset({IssuerId("private_company_a")}),
            )
        )


if __name__ == "__main__":
    pytest_bazel.main()
