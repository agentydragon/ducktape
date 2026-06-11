from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest_bazel
import yaml
from pydantic import TypeAdapter

from finance.augur.fit.main import main as train_main
from finance.augur.model.exogenous import ExogenousSamplingRequest, level_series_request_channels
from finance.augur.model.provider_config import ProviderConfig
from finance.augur.model.series import InflationKey, IssuerId
from finance.augur.model.state_space import StateSpaceProviderConfig

_ADAPTER: TypeAdapter[ProviderConfig] = TypeAdapter(ProviderConfig)


def test_state_space_public_artifact_has_sane_short_horizon_cpi(tmp_path: Path, synthetic_evidence_dir: Path) -> None:
    provider = _train_state_space(tmp_path)
    model = provider.realize_model()
    rollout_count = 500
    sampled = model.sample(
        ExogenousSamplingRequest(
            rollout_seeds=tuple(range(1301, 1301 + rollout_count)),
            horizon_months=6,
            **level_series_request_channels(frozenset({InflationKey()})),
        )
    )

    inflation = sampled.level_matrix(InflationKey(), rollout_count=rollout_count, horizon_months=6)
    six_month_ratio = inflation[:, 6] / inflation[:, 0]
    assert float(np.quantile(six_month_ratio, 0.01)) > 0.95
    assert float(np.quantile(six_month_ratio, 0.99)) < 1.08


def test_state_space_private_equity_artifact_models_price_and_sale_event(
    tmp_path: Path, synthetic_evidence_dir: Path
) -> None:
    private_config = _write_private_equity_fixture(tmp_path)
    provider = _train_state_space(tmp_path, private_equity_config=private_config)
    model = provider.realize_model()
    sampled = model.sample(
        ExogenousSamplingRequest(
            rollout_seeds=(1, 2),
            horizon_months=12,
            required_private_equity_issuers=frozenset({IssuerId("private_company_a")}),
        )
    )

    private_company = sampled.private_equity.issuer_float_matrix(
        "private_company_a", "mark_usd_per_unit", rollout_count=2, horizon_months=12
    )
    np.testing.assert_allclose(private_company[:, 0], np.array([300.0, 300.0]))
    assert sampled.private_equity.issuer_bool_matrix(
        "private_company_a", "sale_opportunity_active", rollout_count=2, horizon_months=12
    ).shape == (2, 13)
    assert sampled.metadata["private_equity_prices_usd"] == {"private_company_a": 300.0}
    source_manifest = cast(dict[str, Any], sampled.metadata["source_manifest"])
    private_sources = cast(dict[str, Any], source_manifest["private_equity"])
    assert "private_equity:private_company_a" in private_sources


def _train_state_space(tmp_path: Path, *, private_equity_config: Path | None = None) -> StateSpaceProviderConfig:
    out_manifest = tmp_path / "state_space_provider.yaml"
    out_blob = tmp_path / "state_space_artifact.json"
    args = ["--model", "state_space", "--out-provider-config", str(out_manifest), "--out-blob", str(out_blob)]
    if private_equity_config is not None:
        args.extend(["--private-equity-config", str(private_equity_config)])
    train_main(args)
    parsed = _ADAPTER.validate_python(yaml.safe_load(out_manifest.read_text(encoding="utf-8")))
    assert isinstance(parsed, StateSpaceProviderConfig)
    return parsed


def _write_private_equity_fixture(tmp_path: Path) -> Path:
    data_path = tmp_path / "private_company_a_observations.jsonl"
    rows = [
        {
            "type": "price_observation",
            "issuer_id": "private_company_a",
            "observed_at": "2024-01-15",
            "kind": "tender_price",
            "price_usd_per_share": 100.0,
            "uncertainty_log_sigma": 0.1,
            "source_id": "fixture:tender_2024",
        },
        {
            "type": "price_observation",
            "issuer_id": "private_company_a",
            "observed_at": "2025-01-15",
            "kind": "tender_price",
            "price_usd_per_share": 200.0,
            "uncertainty_log_sigma": 0.1,
            "source_id": "fixture:tender_2025",
        },
        {
            "type": "price_observation",
            "issuer_id": "private_company_a",
            "observed_at": "2026-04-15",
            "kind": "ppu_mark",
            "price_usd_per_share": 300.0,
            "uncertainty_log_sigma": 0.1,
            "source_id": "fixture:shareworks",
        },
        {
            "type": "valuation_observation",
            "issuer_id": "private_company_a",
            "observed_at": "2026-04-15",
            "valuation_usd": 3_000_000_000.0,
            "uncertainty_log_sigma": 0.1,
            "valuation_kind": "implied",
            "source_id": "fixture:valuation",
        },
    ]
    data_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    config_path = tmp_path / "private_company_a_train.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "issuer_id": "private_company_a",
                "observations_path": str(data_path),
                "out_model_path": str(tmp_path / "unused_private_company_a_model.json"),
                "as_of_date": "2026-04-15",
                "priors": {
                    "macro_capacity_reference_usd": 100_000_000_000.0,
                    "min_monthly_log_return_sigma": 0.03,
                    "student_t_nu": 5.0,
                    "tender_interval_months_median_prior": 9.0,
                    "tender_interval_log_sigma": 0.35,
                    "tender_price_log_discount_mu": 0.0,
                    "tender_price_log_discount_sigma": 0.08,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return config_path


if __name__ == "__main__":
    pytest_bazel.main()
