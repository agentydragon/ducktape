from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import pytest

from finance.augur.api.config import Config, load_augur_config
from finance.augur.fit.synthetic_evidence import write_synthetic_evidence
from finance.augur.model.deterministic import Constant, Deterministic
from finance.augur.model.level_series_groups import AssetPriceGroups
from finance.augur.model.series import IssuerId, PrivateEquityEventKindCode, PrivateEquityRegimeCode, SecuritySymbol
from finance.augur.model.series_model import SeriesModelBundle
from finance.augur.model.testing import (
    ConstantFrameModel,
    PrivateEquityChannels,
    event_matrix_with_month_override,
    int_matrix_with_month_override,
    level_matrix_with_month_override,
)
from finance.augur.product.testing import TEST_CONFIG_LEVEL_PLACEHOLDERS
from finance.augur.sim.locations import Location
from finance.augur.sim.scenario import Agent, InitialAccountBalance, Scenario, ScheduledTransfer
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
        model_id="forced_pe_fixture",
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
        model_id="capacity_limited_pe_fixture",
    )


DeterministicSeriesModelBundleFactory = Callable[[Sequence[float]], SeriesModelBundle]
# Module-level singleton so the fixture's default isn't a call in arg defaults (ruff B008).
_DEFAULT_SYMBOL = SecuritySymbol("vti")
ConstantPriceBundleFactory = Callable[[Mapping[SecuritySymbol, float]], SeriesModelBundle]


@pytest.fixture
def deterministic_series_bundle() -> DeterministicSeriesModelBundleFactory:
    def build(levels: Sequence[float], *, symbol: SecuritySymbol = _DEFAULT_SYMBOL) -> SeriesModelBundle:
        # The fixture's series lives in the asset-price role (keyed by symbol); all
        # callers take the default. No flat LevelSeriesKey map is constructed.
        return SeriesModelBundle.independent(
            asset_prices=AssetPriceGroups(security={symbol: Deterministic(levels=list(levels))})
        )

    return build


@pytest.fixture
def constant_price_bundle() -> ConstantPriceBundleFactory:
    """Asset prices every rollout and month holds flat.

    A sale is priced off its asset's own sampled series, so a test that wants an exact
    expected proceeds figure pins the series rather than the sale.
    """

    def build(prices: Mapping[SecuritySymbol, float]) -> SeriesModelBundle:
        return SeriesModelBundle.independent(
            asset_prices=AssetPriceGroups(security={symbol: Constant(value=price) for symbol, price in prices.items()})
        )

    return build


@pytest.fixture
def san_francisco_location() -> Location:
    return Location(
        location_id="san_francisco",
        display_name="San Francisco, CA",
        jurisdiction_ids=["federal_us", "california"],
        annual_property_tax_rate=0.01180,
        annual_special_assessment=0,
    )


@pytest.fixture
def vallejo_mare_island_location() -> Location:
    return Location(
        location_id="vallejo_mare_island",
        display_name="Vallejo, CA — Mare Island",
        jurisdiction_ids=["federal_us", "california"],
        annual_property_tax_rate=0.0115,
        annual_special_assessment=2300,
    )


@pytest.fixture
def alice_bob_scenario() -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="bob")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=10),
            InitialAccountBalance(agent_id="bob", account_id="checking", balance=20),
        ],
        scheduled_transfers=[
            ScheduledTransfer(
                month=0,
                cause_id="bob_gives_alice_5",
                from_agent_id="bob",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount=5,
            )
        ],
        tax_profiles=[],
        horizon_months=1,
    )
