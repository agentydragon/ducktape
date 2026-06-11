from __future__ import annotations

import pytest

from finance.augur.api.config import Config, load_augur_config
from finance.augur.fit.synthetic_evidence import write_synthetic_evidence
from finance.augur.model.series import IssuerId, PrivateEquityEventKindCode, PrivateEquityRegimeCode
from finance.augur.model.testing import (
    ConstantFrameModel,
    PrivateEquityChannels,
    event_matrix_with_month_override,
    int_matrix_with_month_override,
    level_matrix_with_month_override,
)
from finance.augur.product.testing import TEST_CONFIG_LEVEL_PLACEHOLDERS
from util.bazel.runfiles import get_required_path

_PRIVATE_HOLDING_A = IssuerId("private_holding_a")


@pytest.fixture(autouse=True)
def _augur_evidence_dir(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point AUGUR_EVIDENCE_DIR at a synthetic evidence set for every test.

    The augur server reads exogenous evidence from AUGUR_EVIDENCE_DIR (the git-synced dir in
    prod); any test that exercises the server — the calibration endpoint, the visual goldens —
    needs it set (read lazily per request), so mirror prod and always provide it here."""
    evidence_dir = tmp_path_factory.mktemp("augur_evidence")
    write_synthetic_evidence(evidence_dir)
    monkeypatch.setenv("AUGUR_EVIDENCE_DIR", str(evidence_dir))


@pytest.fixture(scope="module")
def augur_config() -> Config:
    return load_augur_config(get_required_path("_main/finance/augur/api/testdata/config.yaml"))


@pytest.fixture
def forced_private_equity_event_model() -> ConstantFrameModel:
    """Single acquisition-cashout PE event at month 1; non-PE levels at 1.0."""
    return ConstantFrameModel(
        levels=TEST_CONFIG_LEVEL_PLACEHOLDERS,
        private_equity={
            _PRIVATE_HOLDING_A: PrivateEquityChannels(
                mark_usd_per_unit=1.0,
                event_kind_code=int_matrix_with_month_override(
                    default=int(PrivateEquityEventKindCode.NONE),
                    override=int(PrivateEquityEventKindCode.ACQUISITION_CASHOUT),
                    month=1,
                ),
                regime_code=int_matrix_with_month_override(
                    default=int(PrivateEquityRegimeCode.PRIVATE_OPERATING),
                    override=int(PrivateEquityRegimeCode.ACQUIRED),
                    month=1,
                ),
                forced_sale_fraction=level_matrix_with_month_override(default=0.0, override=0.25, month=1),
            )
        },
        metadata={"model_id": "forced_pe_fixture"},
    )


@pytest.fixture
def capacity_limited_private_equity_model() -> ConstantFrameModel:
    """Tender opportunity at month 1 with sale_capacity_fraction=0.25."""
    return ConstantFrameModel(
        levels=TEST_CONFIG_LEVEL_PLACEHOLDERS,
        private_equity={
            _PRIVATE_HOLDING_A: PrivateEquityChannels(
                mark_usd_per_unit=25.0,
                sale_capacity_fraction=0.25,
                sale_opportunity_active=event_matrix_with_month_override(default=False, override=True, month=1),
                event_kind_code=int_matrix_with_month_override(
                    default=int(PrivateEquityEventKindCode.NONE),
                    override=int(PrivateEquityEventKindCode.TENDER),
                    month=1,
                ),
            )
        },
        metadata={"model_id": "capacity_limited_pe_fixture"},
    )
