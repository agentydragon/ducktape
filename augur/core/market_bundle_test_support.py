from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from augur.core.market_bundle import MarketBundle, MarketBundleMetadata, RequiredMarketKeys
from augur.core.scenario_set import MarketRequest


def constant_market_bundle(
    *,
    rollout_count: int = 2,
    horizon_months: int = 3,
    inflation_path: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0),
    sp500_path: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0),
    home_path: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0),
    rent_path: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0),
    mortgage_30y_rate_pct_path: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0),
    private_equity_value_path: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0),
    private_equity_sale_opportunity_months: tuple[int, ...] = (),
    crypto_value_path: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0),
    private_equity_value_paths_by_issuer: dict[str, tuple[float, ...]] | None = None,
    private_equity_sale_opportunity_months_by_issuer: dict[str, tuple[int, ...]] | None = None,
    crypto_value_paths_by_symbol: dict[str, tuple[float, ...]] | None = None,
    location_keys: frozenset[str] = frozenset(),
    pe_issuer_keys: frozenset[str] = frozenset(),
    crypto_symbol_keys: frozenset[str] = frozenset(),
    current_private_equity_price_usd: float = 0.0,
    market_model_id: str = "test",
    seed: int = 0,
) -> MarketBundle:
    """Build a constant-path `MarketBundle` for tests.

    Per-asset path dicts carry exactly the keys the caller declares (via
    `location_keys` / `pe_issuer_keys` / `crypto_symbol_keys`, or the explicit
    `*_paths_by_issuer` / `*_paths_by_symbol` dicts). No fallback population —
    tests should declare what their scenarios actually look up.
    """
    shape = (rollout_count, horizon_months + 1)
    month_index = np.arange(horizon_months + 1, dtype="int64")

    def path(values: tuple[float, ...]) -> np.ndarray:
        source = np.asarray(values, dtype="float64")
        if source.size < horizon_months + 1:
            source = np.pad(source, (0, horizon_months + 1 - source.size), constant_values=source[-1])
        return np.broadcast_to(source[: horizon_months + 1], shape).copy()

    def mask(months: tuple[int, ...]) -> np.ndarray:
        result = np.zeros(shape, dtype=np.bool_)
        for month in months:
            if 0 <= month <= horizon_months:
                result[:, month] = True
        return result

    home = path(home_path)
    rent = path(rent_path)
    private_equity_value = path(private_equity_value_path)
    private_equity_sale_opportunity_mask = mask(private_equity_sale_opportunity_months)
    crypto_value = path(crypto_value_path)

    home_by_location = dict.fromkeys(location_keys, home)
    rent_by_location = dict.fromkeys(location_keys, rent)

    pe_value_by_issuer: dict[str, np.ndarray] = {}
    if private_equity_value_paths_by_issuer is not None:
        for issuer_id, values in private_equity_value_paths_by_issuer.items():
            pe_value_by_issuer[issuer_id] = path(values)
    for key in pe_issuer_keys:
        pe_value_by_issuer.setdefault(key, private_equity_value)

    pe_mask_by_issuer: dict[str, np.ndarray] = {}
    if private_equity_sale_opportunity_months_by_issuer is not None:
        for issuer_id, months in private_equity_sale_opportunity_months_by_issuer.items():
            pe_mask_by_issuer[issuer_id] = mask(months)
    for key in pe_value_by_issuer:
        pe_mask_by_issuer.setdefault(key, private_equity_sale_opportunity_mask)

    crypto_by_symbol: dict[str, np.ndarray] = {}
    if crypto_value_paths_by_symbol is not None:
        for symbol, values in crypto_value_paths_by_symbol.items():
            crypto_by_symbol[symbol] = path(values)
    for key in crypto_symbol_keys:
        crypto_by_symbol.setdefault(key, crypto_value)

    return MarketBundle(
        month_index=month_index,
        inflation_multipliers=path(inflation_path),
        generic_sp500_multipliers=path(sp500_path),
        home_value_multipliers_by_location=home_by_location,
        rent_multipliers_by_location=rent_by_location,
        mortgage_30y_rate_pct=path(mortgage_30y_rate_pct_path),
        private_equity_value_multipliers_by_issuer=pe_value_by_issuer,
        private_equity_sale_opportunity_mask_by_issuer=pe_mask_by_issuer,
        crypto_value_multipliers_by_symbol=crypto_by_symbol,
        metadata=MarketBundleMetadata(
            market_model_id=market_model_id,
            seed=seed,
            rollout_count=rollout_count,
            horizon_months=horizon_months,
            event_stream_ids=(),
            current_private_equity_price_usd=current_private_equity_price_usd,
        ),
    )


@dataclass(frozen=True)
class NoopMarketBundleProvider:
    """Deterministic market provider for tests."""

    inflation_path: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0)
    sp500_path: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0)
    home_path: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0)
    rent_path: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0)
    mortgage_30y_rate_pct_path: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0)
    private_equity_value_path: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0)
    private_equity_sale_opportunity_months: tuple[int, ...] = ()
    crypto_value_path: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0)
    private_equity_value_paths_by_issuer: dict[str, tuple[float, ...]] = field(default_factory=dict)
    private_equity_sale_opportunity_months_by_issuer: dict[str, tuple[int, ...]] = field(default_factory=dict)
    crypto_value_paths_by_symbol: dict[str, tuple[float, ...]] = field(default_factory=dict)
    current_private_equity_price_usd: float = 0.0

    def sample_market_bundle(
        self,
        *,
        rollout_count: int,
        horizon_months: int,
        seed: int,
        market_request: MarketRequest,
        required_keys: RequiredMarketKeys,
    ) -> MarketBundle:
        return constant_market_bundle(
            rollout_count=rollout_count,
            horizon_months=horizon_months,
            inflation_path=self.inflation_path,
            sp500_path=self.sp500_path,
            home_path=self.home_path,
            rent_path=self.rent_path,
            mortgage_30y_rate_pct_path=self.mortgage_30y_rate_pct_path,
            private_equity_value_path=self.private_equity_value_path,
            private_equity_sale_opportunity_months=self.private_equity_sale_opportunity_months,
            crypto_value_path=self.crypto_value_path,
            private_equity_value_paths_by_issuer=self.private_equity_value_paths_by_issuer or None,
            private_equity_sale_opportunity_months_by_issuer=(
                self.private_equity_sale_opportunity_months_by_issuer or None
            ),
            crypto_value_paths_by_symbol=self.crypto_value_paths_by_symbol or None,
            location_keys=required_keys.location_ids,
            pe_issuer_keys=required_keys.pe_issuer_ids,
            crypto_symbol_keys=required_keys.crypto_symbols,
            current_private_equity_price_usd=self.current_private_equity_price_usd,
            market_model_id=market_request.market_model_id,
            seed=seed,
        )
