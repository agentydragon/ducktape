"""Shared product-side test data.

The constant-frame sampler and matrix-builder helpers live in `augur.model.testing` (one
layer down) so model-layer tests can use them too. This module hosts the level-series
placeholders for the holdings in `augur/api/testdata/config.yaml`; the prebuilt PE-event
samplers reused across the api/product test trees are fixtures in `augur/conftest.py`.
"""

from __future__ import annotations

from collections.abc import Mapping

from finance.augur.model.series import CryptoKey, CryptoSymbol, LevelSeriesKey, SP500Key

# Placeholder values for the level series the test config
# (`augur/api/testdata/config.yaml`) holds — sp500, crypto:btc, crypto:eth.
# Tests that exercise PE behavior only need these present; tests that
# exercise crypto/sp500 price behavior seed their own values explicitly.
TEST_CONFIG_LEVEL_PLACEHOLDERS: Mapping[LevelSeriesKey, float] = {
    SP500Key(): 1.0,
    CryptoKey(symbol=CryptoSymbol("btc")): 1.0,
    CryptoKey(symbol=CryptoSymbol("eth")): 1.0,
}
