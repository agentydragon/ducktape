"""Construct current public-security proxies from already materialized markets.

No loading, fitting, sampling or tax settlement. Bond funds retain same-maturity
repricing, monthly coupons and positive-yield guards. Equity is a total-return
price proxy with no dividend payout; it is not a tax-faithful distributing fund.
These current approximations are not correctness or compatibility constraints;
independently justified financial corrections can change them separately.
"""

from collections import Counter
from collections.abc import Sequence

import numpy as np

from finance.augur.model.bond_fund import (
    MINIMUM_ANNUAL_YIELD,
    BondFundSpec,
    YieldCurve,
    constant_maturity_fund_paths,
    fund_yield,
    government_curve_yield,
)
from finance.augur.model.equity import EquitySpec
from finance.augur.model.exogenous import SampledExogenousBundle, assemble_level_frames
from finance.augur.model.market_paths import MarketPaths
from finance.augur.model.series import InflationKey, LevelSeriesKey, SecurityDistributionKey, SecurityKey


def validate_product_symbols(*, equity: EquitySpec | None, instruments: Sequence[BondFundSpec]) -> None:
    symbols = [spec.symbol for spec in instruments]
    if equity is not None:
        symbols.append(equity.symbol)
    duplicates = sorted(symbol for symbol, count in Counter(symbols).items() if count > 1)
    if duplicates:
        raise ValueError(f"construction prices a symbol more than once: {duplicates}")


def construct_products(
    paths: MarketPaths, *, equity: EquitySpec | None, instruments: Sequence[BondFundSpec]
) -> SampledExogenousBundle:
    """Bind selected proxy instruments; repeated calls neither resample nor mutate paths."""

    validate_product_symbols(equity=equity, instruments=instruments)
    short_rate = np.maximum(paths.short_rate, MINIMUM_ANNUAL_YIELD)
    blocks: list[tuple[LevelSeriesKey, np.ndarray]] = [(InflationKey(), paths.cpi_level)]
    for spec in instruments:
        if spec.yield_curve is YieldCurve.GOVERNMENT:
            reference_yield = government_curve_yield(short_rate, paths.term_spread, maturity_years=spec.maturity_years)
        else:
            if spec.yield_curve not in paths.corporate_yields:
                raise ValueError(
                    f"{spec.symbol} prices off {spec.yield_curve}, which {paths.model_id} cannot produce: "
                    "these paths have no credit factor or observed corporate yield"
                )
            reference_yield = paths.corporate_yields[spec.yield_curve]
        price, distribution = constant_maturity_fund_paths(
            fund_yield(spec, reference_yield),
            maturity_years=spec.maturity_years,
            initial_price_usd=spec.initial_price_usd,
        )
        blocks.append((SecurityKey(symbol=spec.symbol), price))
        blocks.append((SecurityDistributionKey(symbol=spec.symbol), distribution))
    if equity is not None:
        if paths.equity_total_return_index is None:
            raise ValueError(f"{paths.model_id} has no equity total-return path for {equity.symbol}")
        blocks.append((SecurityKey(symbol=equity.symbol), equity.initial_price_usd * paths.equity_total_return_index))
    return SampledExogenousBundle(
        levels=assemble_level_frames(blocks, rollout_count=paths.rollout_count, horizon_months=paths.horizon_months),
        model_id=paths.model_id,
        provenance={**paths.provenance, "instruments": tuple(spec.symbol for spec in instruments)},
    )
