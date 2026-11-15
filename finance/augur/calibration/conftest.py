from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import numpy.typing as npt
import pytest

from finance.augur.calibration.calibration import build_anchored_level_paths
from finance.augur.calibration.catalog import (
    CatalogMetadata,
    MarketCatalog,
    ThresholdLadderFamily,
    ThresholdLadderMember,
)
from finance.augur.calibration.platform import Direction
from finance.augur.fit.synthetic_evidence import synthetic_evidence_dir
from finance.augur.model.exogenous import ExogenousSamplingRequest, SampledExogenousBundle
from finance.augur.model.series import (
    SP500_SYMBOL,
    InflationKey,
    IssuerId,
    LevelSeriesKey,
    PrivateEquityEventKindCode,
    SecurityKey,
)
from finance.augur.model.testing import ConstantFrameModel, PrivateEquityChannels
from finance.evidence.markets import Platform

_SP500_ANCHOR = 6000.0
_ISSUER = IssuerId("issuer_x")


@dataclass(frozen=True)
class CpiLadder:
    catalog: MarketCatalog
    seeds: tuple[int, ...]
    sampled: SampledExogenousBundle
    level_paths: dict[LevelSeriesKey, npt.NDArray[np.float64]]


def _event_kind_codes(request: ExogenousSamplingRequest) -> npt.NDArray[np.int64]:
    """Four rollouts: IPO at months 7 and 60, collapse at month 3, and no event."""
    none = int(PrivateEquityEventKindCode.NONE)
    events = np.full((request.rollout_count, request.horizon_months + 1), none, dtype=np.int64)
    events[0, 7] = int(PrivateEquityEventKindCode.PUBLIC_MARKET_OPEN)
    events[1, 60] = int(PrivateEquityEventKindCode.PUBLIC_MARKET_OPEN)
    events[2, 3] = int(PrivateEquityEventKindCode.COLLAPSE)
    return events


def _inflation_levels(request: ExogenousSamplingRequest) -> npt.NDArray[np.float64]:
    """Four rollouts: month 7 YoY rates = [2.0%, 3.25%, 3.75%, 5.0%] against history=100."""
    matrix = np.full((request.rollout_count, request.horizon_months + 1), 100.0, dtype=np.float64)
    matrix[:, 7] = [102.0, 103.25, 103.75, 105.0]
    return matrix


def _sp500_levels(request: ExogenousSamplingRequest) -> npt.NDArray[np.float64]:
    """Four rollouts: month 0 = anchor; month 7 = [7000, 7600, 8000, 5000]."""
    matrix = np.full((request.rollout_count, request.horizon_months + 1), _SP500_ANCHOR, dtype=np.float64)
    matrix[:, 7] = [7000.0, 7600.0, 8000.0, 5000.0]
    return matrix


@pytest.fixture
def model() -> ConstantFrameModel:
    return ConstantFrameModel(
        private_equity={_ISSUER: PrivateEquityChannels(mark_usd_per_unit=50.0, event_kind_code=_event_kind_codes)}
    )


@pytest.fixture
def macro_model(model: ConstantFrameModel) -> ConstantFrameModel:
    model.levels = {SecurityKey(symbol=SP500_SYMBOL): _sp500_levels}
    return model


@pytest.fixture
def cpi_ladder(model: ConstantFrameModel) -> CpiLadder:
    model.levels = {InflationKey(): _inflation_levels}
    catalog = MarketCatalog(
        metadata=CatalogMetadata(as_of=date(2026, 5, 27), anchors={"inflation": 100.0}),
        markets=[],
        threshold_ladder_families=[
            ThresholdLadderFamily(
                family_id="cpi_yoy",
                question="CPI YoY threshold ladder",
                platform=Platform.KALSHI,
                series="inflation",
                value_kind="inflation_yoy",
                direction=Direction.ABOVE,
                at_date=date(2026, 12, 31),
                thresholds=[
                    ThresholdLadderMember(market_id="CPI-T3.0", threshold=0.03),
                    ThresholdLadderMember(market_id="CPI-T3.5", threshold=0.035),
                    ThresholdLadderMember(market_id="CPI-T4.0", threshold=0.04),
                ],
            )
        ],
    )
    seeds = tuple(range(4))
    sampled = model.sample(
        ExogenousSamplingRequest(
            horizon_months=120,
            rollout_seeds=seeds,
            required_index_series=frozenset({InflationKey()}),
            required_private_equity_issuers=frozenset({_ISSUER}),
        )
    )
    paths = build_anchored_level_paths(
        sampled,
        anchors={"inflation": 100.0},
        requested_wire_ids=catalog.referenced_level_series(),
        rollout_count=4,
        horizon_months=120,
    )
    return CpiLadder(catalog, seeds, sampled, paths)
