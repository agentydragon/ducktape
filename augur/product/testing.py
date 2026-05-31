"""Shared product-side test fixtures.

The constant-frame sampler and matrix-builder helpers live in
`augur.model.testing` (one layer down) so model-layer tests can use them
too. This module hosts fixtures that depend on the product layer or on
the canonical Augur test config — most notably level-series placeholders
for the holdings in `augur/api/testdata/config.yaml` and prebuilt PE
scenarios reused across `augur/api:server_test` and
`augur/product:service_test`.
"""

from __future__ import annotations

from collections.abc import Mapping

from augur.model.series import (
    CryptoKey,
    CryptoSymbol,
    IssuerId,
    LevelSeriesKey,
    PrivateEquityEventKindCode,
    PrivateEquityRegimeCode,
    SP500Key,
)
from augur.model.testing import (
    ConstantFrameModel,
    PrivateEquityChannels,
    event_matrix_with_month_override,
    int_matrix_with_month_override,
    level_matrix_with_month_override,
)

# Placeholder values for the level series the test config
# (`augur/api/testdata/config.yaml`) holds — sp500, crypto:btc, crypto:eth.
# Tests that exercise PE behavior only need these present; tests that
# exercise crypto/sp500 price behavior seed their own values explicitly.
TEST_CONFIG_LEVEL_PLACEHOLDERS: Mapping[LevelSeriesKey, float] = {
    SP500Key(): 1.0,
    CryptoKey(symbol=CryptoSymbol("btc")): 1.0,
    CryptoKey(symbol=CryptoSymbol("eth")): 1.0,
}

_PRIVATE_HOLDING_A = IssuerId("private_holding_a")


def forced_private_equity_event_fixture() -> ConstantFrameModel:
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


def capacity_limited_private_equity_fixture() -> ConstantFrameModel:
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
