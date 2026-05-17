from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from augur.core.local_regulation import LocationId
from augur.core.market_bundle import MarketBundle, MarketBundleMetadata
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
    current_private_equity_price_usd: float = 0.0,
    market_model_id: str = "test",
    seed: int = 0,
) -> MarketBundle:
    shape = (rollout_count, horizon_months + 1)
    month_index = np.arange(horizon_months + 1, dtype="int64")

    def path(values: tuple[float, ...]) -> np.ndarray:
        source = np.asarray(values, dtype="float64")
        if source.size < horizon_months + 1:
            source = np.pad(source, (0, horizon_months + 1 - source.size), constant_values=source[-1])
        return np.broadcast_to(source[: horizon_months + 1], shape).copy()

    home = path(home_path)
    rent = path(rent_path)
    private_equity_sale_opportunity_mask = np.zeros(shape, dtype=np.bool_)
    for month in private_equity_sale_opportunity_months:
        if 0 <= month <= horizon_months:
            private_equity_sale_opportunity_mask[:, month] = True
    return MarketBundle(
        month_index=month_index,
        inflation_multipliers=path(inflation_path),
        generic_sp500_multipliers=path(sp500_path),
        home_value_multipliers_by_location={
            "default": home,
            LocationId.SAN_FRANCISCO_CA: home,
            LocationId.VALLEJO_CA: home,
            LocationId.MARE_ISLAND_VALLEJO_CA: home,
        },
        rent_multipliers_by_location={
            "default": rent,
            LocationId.SAN_FRANCISCO_CA: rent,
            LocationId.VALLEJO_CA: rent,
            LocationId.MARE_ISLAND_VALLEJO_CA: rent,
        },
        mortgage_30y_rate_pct=path(mortgage_30y_rate_pct_path),
        private_equity_value_multipliers=path(private_equity_value_path),
        private_equity_sale_opportunity_mask=private_equity_sale_opportunity_mask,
        crypto_value_multipliers=path(crypto_value_path),
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
    current_private_equity_price_usd: float = 0.0

    def sample_market_bundle(
        self, *, rollout_count: int, horizon_months: int, seed: int, market_request: MarketRequest
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
            current_private_equity_price_usd=self.current_private_equity_price_usd,
            market_model_id=market_request.market_model_id,
            seed=seed,
        )
