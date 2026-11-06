from __future__ import annotations

from collections.abc import Callable

import pytest
from python.runfiles import Runfiles

from finance.augur.api.config import Config, load_augur_config
from finance.augur.api.portfolio import BondHoldingConfig, PortfolioAccountConfig, PortfolioConfig
from finance.augur.fit.synthetic_evidence import synthetic_evidence_dir
from finance.augur.model.series import IssuerId, PrivateEquityEventKindCode, PrivateEquityRegimeCode
from finance.augur.model.testing import (
    ConstantFrameModel,
    PrivateEquityChannels,
    event_matrix_with_month_override,
    int_matrix_with_month_override,
    level_matrix_with_month_override,
)
from finance.augur.product.testing import TEST_CONFIG_LEVEL_PLACEHOLDERS
from finance.augur.sim.ids import AccountId, AgentId

_PRIVATE_HOLDING_A = IssuerId("private_holding_a")


@pytest.fixture(scope="module")
def augur_config() -> Config:
    return load_augur_config(Runfiles.CreateOrRaise().root() / "ducktape/finance/augur/api/testdata/config.yaml")


@pytest.fixture
def bond_portfolio() -> Callable[..., PortfolioConfig]:
    def make(**overrides: object) -> PortfolioConfig:
        bond = {
            "bond_id": "tips_rung",
            "account_id": "brokerage",
            "character": {"kind": "treasury"},
            "face_value": 100_000,
            "purchase_price": 100_000,
            "annual_coupon_rate": 0.02,
            "inflation_indexed": True,
            "holding_period_months_at_start": 24,
            "months_to_maturity_at_start": 96,
        } | overrides
        return PortfolioConfig(
            accounts=(PortfolioAccountConfig(account_id=AccountId("brokerage"), owner_agent_id=AgentId("alice")),),
            bonds=(BondHoldingConfig.model_validate(bond),),
        )

    return make


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
    )
