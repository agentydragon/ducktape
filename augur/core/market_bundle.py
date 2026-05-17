from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import numpy as np
from pydantic import Field, computed_field

from augur.core.provenance import (
    CalibrationArtifact,
    CalibrationRun,
    EvidenceSet,
    ExogenousPathSet,
    KnownLimitation,
    ModelCard,
    ScenarioGeneratorRun,
    ValidationReport,
    calibration_run_id,
    exogenous_path_id,
    path_set_id,
    scenario_generator_run_id,
)
from augur.core.scenario_set import MarketRequest
from augur.core.schemas import ApiModel

CORE_MARKET_RISK_FACTOR_IDS = (
    "inflation",
    "sp500",
    "home",
    "rent",
    "mortgage_30y_rate_pct",
    "private_equity_value",
    "crypto_value",
)


class MarketBundleMetadata(ApiModel):
    market_model_id: str
    model_card_id: str | None = None
    model_version_id: str | None = None
    validation_report_id: str | None = None
    known_limitation_ids: tuple[str, ...] = ()
    market_model_version_id: str = "unknown"
    scenario_generator_id: str = "market_bundle_provider"
    scenario_generator_version_id: str = "unknown"
    evidence_set_id: str = "unknown"
    calibration_artifact_id: str = "unknown"
    risk_factor_set_id: str = "core_market_factors:v1"
    risk_factor_ids: tuple[str, ...] = ()
    evidence_latest_observation_ids: tuple[str, ...] = ()
    seed: int
    rollout_count: int
    horizon_months: int
    event_stream_ids: tuple[str, ...]
    notes: tuple[str, ...] = ()
    current_private_equity_price_usd: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Per-unit USD mark for private equity at month 0. Used by the simulator to "
            "resolve `PrivateEquityPosition.value_usd` from `units` when the position omits "
            "an explicit mark. Providers that drive PE valuation must set this; the flat/"
            "noop fixtures use 0.0 and require positions to supply `value_usd` directly."
        ),
    )
    source_metadata: dict[str, Any] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def path_set_id(self) -> str:
        return path_set_id(
            market_model_id=self.market_model_id,
            market_model_version_id=self.market_model_version_id,
            scenario_generator_id=self.scenario_generator_id,
            scenario_generator_version_id=self.scenario_generator_version_id,
            evidence_set_id=self.evidence_set_id,
            calibration_artifact_id=self.calibration_artifact_id,
            risk_factor_set_id=self.risk_factor_set_id,
            seed=self.seed,
            rollout_count=self.rollout_count,
            horizon_months=self.horizon_months,
            event_stream_ids=self.event_stream_ids,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def model_card(self) -> ModelCard | None:
        if self.model_card_id is None:
            return None
        return ModelCard(model_card_id=self.model_card_id, model_version_id=self.model_version_id)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def validation_report(self) -> ValidationReport | None:
        if self.validation_report_id is None:
            return None
        return ValidationReport(
            validation_report_id=self.validation_report_id,
            model_version_id=self.model_version_id or self.market_model_version_id,
            evidence_set_id=self.evidence_set_id,
            calibration_artifact_id=self.calibration_artifact_id,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def known_limitations(self) -> tuple[KnownLimitation, ...]:
        return tuple(KnownLimitation(known_limitation_id=limitation_id) for limitation_id in self.known_limitation_ids)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def evidence_set(self) -> EvidenceSet:
        return EvidenceSet(
            evidence_set_id=self.evidence_set_id,
            risk_factor_set_id=self.risk_factor_set_id,
            factor_ids=self.risk_factor_ids,
            latest_observation_ids=self.evidence_latest_observation_ids,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def calibration_run(self) -> CalibrationRun:
        return CalibrationRun(
            calibration_run_id=calibration_run_id(
                model_version_id=self.market_model_version_id,
                evidence_set_id=self.evidence_set_id,
                risk_factor_set_id=self.risk_factor_set_id,
            ),
            model_version_id=self.market_model_version_id,
            evidence_set_id=self.evidence_set_id,
            risk_factor_set_id=self.risk_factor_set_id,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def calibration_artifact(self) -> CalibrationArtifact:
        return CalibrationArtifact(
            calibration_artifact_id=self.calibration_artifact_id,
            calibration_run_id=self.calibration_run.calibration_run_id,
            model_version_id=self.market_model_version_id,
            evidence_set_id=self.evidence_set_id,
            risk_factor_set_id=self.risk_factor_set_id,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def scenario_generator_run(self) -> ScenarioGeneratorRun:
        return ScenarioGeneratorRun(
            scenario_generator_run_id=scenario_generator_run_id(
                market_model_id=self.market_model_id,
                model_version_id=self.market_model_version_id,
                scenario_generator_id=self.scenario_generator_id,
                scenario_generator_version_id=self.scenario_generator_version_id,
                evidence_set_id=self.evidence_set_id,
                calibration_artifact_id=self.calibration_artifact_id,
                risk_factor_set_id=self.risk_factor_set_id,
                seed=self.seed,
                rollout_count=self.rollout_count,
                horizon_months=self.horizon_months,
                event_stream_ids=self.event_stream_ids,
            ),
            scenario_generator_id=self.scenario_generator_id,
            scenario_generator_version_id=self.scenario_generator_version_id,
            market_model_id=self.market_model_id,
            model_version_id=self.market_model_version_id,
            evidence_set_id=self.evidence_set_id,
            calibration_artifact_id=self.calibration_artifact_id,
            risk_factor_set_id=self.risk_factor_set_id,
            seed=self.seed,
            rollout_count=self.rollout_count,
            horizon_months=self.horizon_months,
            event_stream_ids=self.event_stream_ids,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def exogenous_path_set(self) -> ExogenousPathSet:
        return ExogenousPathSet(
            path_set_id=self.path_set_id,
            scenario_generator_run_id=self.scenario_generator_run.scenario_generator_run_id,
            rollout_count=self.rollout_count,
            horizon_months=self.horizon_months,
            event_stream_ids=self.event_stream_ids,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def exogenous_path_ids(self) -> tuple[str, ...]:
        return tuple(
            exogenous_path_id(path_set_id=self.path_set_id, rollout_index=rollout_index)
            for rollout_index in range(self.rollout_count)
        )

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class MissingMarketFactorError(KeyError):
    """Raised when a scenario looks up a per-asset market factor the bundle does not carry.

    Scenarios declare which keys they need via `RequiredMarketKeys`; providers must
    populate every required key. Any miss is a contract violation between the
    scenario set and the market provider, not a "use a placeholder" condition.
    """

    def __init__(self, *, factor_name: str, key: str, available_keys: tuple[str, ...]) -> None:
        self.factor_name = factor_name
        self.key = key
        self.available_keys = available_keys
        super().__init__(
            f"missing {factor_name} market path for key {key!r}; available={list(available_keys)}. "
            "Scenarios must declare required keys up front (via RequiredMarketKeys) so the "
            "market provider can populate them; there is no fallback path."
        )


@dataclass(frozen=True)
class RequiredMarketKeys:
    """Per-scenario-set declaration of which keyed market paths the run needs.

    `simulate_set` extracts these from the scenario set and passes them to the
    `MarketBundleProvider.sample_market_bundle` call so the provider can populate
    exactly those keys (and raise if it cannot model one of them).
    """

    location_ids: frozenset[str] = frozenset()
    pe_issuer_ids: frozenset[str] = frozenset()
    crypto_symbols: frozenset[str] = frozenset()


@dataclass(frozen=True)
class MarketBundle:
    """Shared sampled market paths for a scenario set.

    Arrays are shaped `(rollout, month)`, where month includes the initial
    month 0. The simulator consumes these arrays directly; conversion to
    JSON-safe columnar payloads happens only at the report boundary.

    All per-asset / per-location paths are keyed explicitly: scenarios declare
    which keys they need (via `RequiredMarketKeys`); the provider populates
    exactly those keys. There is no `"default"` fallback — looking up a missing
    key raises `MissingMarketFactorError`.
    """

    month_index: np.ndarray
    inflation_multipliers: np.ndarray
    generic_sp500_multipliers: np.ndarray
    home_value_multipliers_by_location: dict[str, np.ndarray]
    rent_multipliers_by_location: dict[str, np.ndarray]
    mortgage_30y_rate_pct: np.ndarray
    private_equity_value_multipliers_by_issuer: dict[str, np.ndarray]
    private_equity_sale_opportunity_mask_by_issuer: dict[str, np.ndarray]
    crypto_value_multipliers_by_symbol: dict[str, np.ndarray]
    metadata: MarketBundleMetadata

    def __post_init__(self) -> None:
        expected_shape = (self.rollout_count, self.horizon_months + 1)
        if self.month_index.shape != (self.horizon_months + 1,):
            raise ValueError(f"month_index must be shaped ({self.horizon_months + 1},), got {self.month_index.shape}")
        if not np.array_equal(self.month_index, np.arange(self.horizon_months + 1, dtype="int64")):
            raise ValueError("month_index must be contiguous months starting at 0")

        self._validate_multiplier(
            self.inflation_multipliers, name="inflation_multipliers", expected_shape=expected_shape
        )
        self._validate_multiplier(
            self.generic_sp500_multipliers, name="generic_sp500_multipliers", expected_shape=expected_shape
        )
        self._validate_float_matrix(
            self.mortgage_30y_rate_pct, name="mortgage_30y_rate_pct", expected_shape=expected_shape
        )

        # Per-asset dicts are required to match `RequiredMarketKeys` declared by the
        # scenario set — including the legitimate "no scenarios use this asset class"
        # case, where the dict is empty. Mismatch is caught at lookup time via
        # `MissingMarketFactorError`, so no _require_nonempty baseline here.

        for name, values in self.home_value_multipliers_by_location.items():
            self._validate_multiplier(
                values, name=f"home_value_multipliers_by_location[{name!r}]", expected_shape=expected_shape
            )
        for name, values in self.rent_multipliers_by_location.items():
            self._validate_multiplier(
                values, name=f"rent_multipliers_by_location[{name!r}]", expected_shape=expected_shape
            )
        for name, values in self.private_equity_value_multipliers_by_issuer.items():
            self._validate_multiplier(
                values, name=f"private_equity_value_multipliers_by_issuer[{name!r}]", expected_shape=expected_shape
            )
        for name, values in self.private_equity_sale_opportunity_mask_by_issuer.items():
            self._validate_bool_matrix(
                values, name=f"private_equity_sale_opportunity_mask_by_issuer[{name!r}]", expected_shape=expected_shape
            )
        for name, values in self.crypto_value_multipliers_by_symbol.items():
            self._validate_multiplier(
                values, name=f"crypto_value_multipliers_by_symbol[{name!r}]", expected_shape=expected_shape
            )

    @property
    def rollout_count(self) -> int:
        return self.metadata.rollout_count

    @property
    def horizon_months(self) -> int:
        return self.metadata.horizon_months

    def home_value_multipliers(self, location_id: str) -> np.ndarray:
        return self._keyed_path(self.home_value_multipliers_by_location, location_id, factor_name="home_value")

    def rent_multipliers(self, location_id: str) -> np.ndarray:
        return self._keyed_path(self.rent_multipliers_by_location, location_id, factor_name="rent")

    def private_equity_value_multiplier(self, issuer_id: str) -> np.ndarray:
        return self._keyed_path(
            self.private_equity_value_multipliers_by_issuer, issuer_id, factor_name="private_equity_value"
        )

    def private_equity_sale_opportunity_mask_for(self, issuer_id: str) -> np.ndarray:
        return self._keyed_path(
            self.private_equity_sale_opportunity_mask_by_issuer,
            issuer_id,
            factor_name="private_equity_sale_opportunity_mask",
        )

    def crypto_value_multiplier(self, symbol: str) -> np.ndarray:
        return self._keyed_path(self.crypto_value_multipliers_by_symbol, symbol, factor_name="crypto_value")

    @staticmethod
    def _keyed_path(paths: dict[str, np.ndarray], key: str, *, factor_name: str) -> np.ndarray:
        try:
            return paths[key]
        except KeyError as error:
            raise MissingMarketFactorError(
                factor_name=factor_name, key=key, available_keys=tuple(sorted(paths))
            ) from error

    @staticmethod
    def _validate_float_matrix(values: np.ndarray, *, name: str, expected_shape: tuple[int, int]) -> None:
        if not isinstance(values, np.ndarray):
            raise TypeError(f"{name} must be a numpy ndarray")
        if values.shape != expected_shape:
            raise ValueError(f"{name} must be shaped {expected_shape}, got {values.shape}")
        if not np.issubdtype(values.dtype, np.number):
            raise TypeError(f"{name} must have a numeric dtype")
        if not np.all(np.isfinite(values)):
            raise ValueError(f"{name} contains non-finite values")

    @classmethod
    def _validate_multiplier(cls, values: np.ndarray, *, name: str, expected_shape: tuple[int, int]) -> None:
        cls._validate_float_matrix(values, name=name, expected_shape=expected_shape)
        if np.any(values <= 0):
            raise ValueError(f"{name} must be positive")
        if not np.allclose(values[:, 0], 1.0):
            raise ValueError(f"{name} must start at 1.0 in month 0")

    @staticmethod
    def _validate_bool_matrix(values: np.ndarray, *, name: str, expected_shape: tuple[int, int]) -> None:
        if not isinstance(values, np.ndarray):
            raise TypeError(f"{name} must be a numpy ndarray")
        if values.shape != expected_shape:
            raise ValueError(f"{name} must be shaped {expected_shape}, got {values.shape}")
        if values.dtype != np.bool_:
            raise TypeError(f"{name} must have bool dtype")


class MarketBundleProvider(Protocol):
    def sample_market_bundle(
        self,
        *,
        rollout_count: int,
        horizon_months: int,
        seed: int,
        market_request: MarketRequest,
        required_keys: RequiredMarketKeys,
    ) -> MarketBundle: ...


@runtime_checkable
class HorizonBoundMarketBundleProvider(MarketBundleProvider, Protocol):
    horizon_months: int


def sample_market_bundle_for_request(
    provider: MarketBundleProvider, market_request: MarketRequest, *, required_keys: RequiredMarketKeys
) -> MarketBundle:
    return provider.sample_market_bundle(
        rollout_count=int(market_request.rollout_count),
        horizon_months=int(market_request.horizon_months),
        seed=market_request.seed,
        market_request=market_request,
        required_keys=required_keys,
    )


@dataclass(frozen=True)
class FlatMarketBundleProvider:
    """Deterministic flat market provider for fixture-backed app/e2e runs."""

    mortgage_30y_rate_pct: float = 6.5
    private_equity_sale_opportunity_months: tuple[int, ...] = (12,)
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
        shape = (rollout_count, horizon_months + 1)
        flat = np.ones(shape, dtype="float64")
        mortgage_rate = np.full(shape, self.mortgage_30y_rate_pct, dtype="float64")
        private_equity_events = np.zeros(shape, dtype=np.bool_)
        for month in self.private_equity_sale_opportunity_months:
            if 0 <= month <= horizon_months:
                private_equity_events[:, month] = True
        # The flat provider treats every location/issuer/symbol identically, so each
        # required key gets the same flat array. No fallback for empty required-keys —
        # if the scenario set declares no PE/crypto/location keys, the corresponding
        # dict is legitimately empty.
        home_by_location = dict.fromkeys(required_keys.location_ids, flat)
        rent_by_location = dict.fromkeys(required_keys.location_ids, flat)
        pe_value_by_issuer = dict.fromkeys(required_keys.pe_issuer_ids, flat)
        pe_mask_by_issuer = dict.fromkeys(required_keys.pe_issuer_ids, private_equity_events)
        crypto_by_symbol = dict.fromkeys(required_keys.crypto_symbols, flat)
        return MarketBundle(
            month_index=np.arange(horizon_months + 1, dtype="int64"),
            inflation_multipliers=flat,
            generic_sp500_multipliers=flat,
            home_value_multipliers_by_location=home_by_location,
            rent_multipliers_by_location=rent_by_location,
            mortgage_30y_rate_pct=mortgage_rate,
            private_equity_value_multipliers_by_issuer=pe_value_by_issuer,
            private_equity_sale_opportunity_mask_by_issuer=pe_mask_by_issuer,
            crypto_value_multipliers_by_symbol=crypto_by_symbol,
            metadata=MarketBundleMetadata(
                market_model_id=market_request.market_model_id,
                scenario_generator_id="flat_market_bundle_provider",
                scenario_generator_version_id="flat_market_bundle_provider:v1",
                evidence_set_id="fixture:flat",
                calibration_artifact_id="fixture:flat",
                risk_factor_ids=CORE_MARKET_RISK_FACTOR_IDS,
                current_private_equity_price_usd=self.current_private_equity_price_usd,
                seed=seed,
                rollout_count=rollout_count,
                horizon_months=horizon_months,
                event_stream_ids=("private_equity_sale_opportunity_event",),
                notes=("deterministic flat provider for fixture-backed app/e2e runs",),
            ),
        )


class SimpleLocationModelParams(ApiModel):
    """Per-location knobs for the simple lognormal market model.

    A single annual percentage-point spread (positive or negative) is applied
    on top of the model's base home / rent multiplier paths via
    `(1 + adj/100)^(months/12)`. Zero (the default for either field) means the
    location rides the unadjusted base path."""

    home_value_annual_adjustment_pct: float = 0.0
    rent_annual_adjustment_pct: float = 0.0


class SimpleMarketModelConfig(ApiModel):
    """Deployment-supplied parameters for `SimpleMarketBundleProvider`.

    Keys in `location_params` must be a subset of the location ids declared on
    the scenario set's `required_keys.location_ids`; an adjustment for a
    location that isn't required is an error (the provider would be inventing
    paths the scenario didn't ask for). Required locations missing from the
    dict run with the default `SimpleLocationModelParams()` — i.e., the
    model's "no per-location opinion" stance.
    """

    location_params: dict[str, SimpleLocationModelParams] = Field(default_factory=dict)


@dataclass(frozen=True)
class SimpleMarketBundleProvider:
    """Small stochastic provider used until richer market models plug in."""

    current_private_equity_price_usd: float = 0.0
    model_config: SimpleMarketModelConfig = field(default_factory=SimpleMarketModelConfig)

    def sample_market_bundle(
        self,
        *,
        rollout_count: int,
        horizon_months: int,
        seed: int,
        market_request: MarketRequest,
        required_keys: RequiredMarketKeys,
    ) -> MarketBundle:
        rng = np.random.default_rng(seed)
        month_index = np.arange(horizon_months + 1, dtype="int64")
        inflation = _lognormal_multiplier_paths(
            rng,
            rollout_count=rollout_count,
            horizon_months=horizon_months,
            annual_return_pct=3.0,
            annual_volatility_pct=1.5,
        )
        sp500 = _lognormal_multiplier_paths(
            rng,
            rollout_count=rollout_count,
            horizon_months=horizon_months,
            annual_return_pct=7.0,
            annual_volatility_pct=16.0,
        )
        private_equity_value = _lognormal_multiplier_paths(
            rng,
            rollout_count=rollout_count,
            horizon_months=horizon_months,
            annual_return_pct=8.0,
            annual_volatility_pct=35.0,
        )
        home_base = _lognormal_multiplier_paths(
            rng,
            rollout_count=rollout_count,
            horizon_months=horizon_months,
            annual_return_pct=3.5,
            annual_volatility_pct=8.0,
        )
        rent_base = _lognormal_multiplier_paths(
            rng,
            rollout_count=rollout_count,
            horizon_months=horizon_months,
            annual_return_pct=3.0,
            annual_volatility_pct=3.0,
        )
        mortgage_rate = _mortgage_rate_paths(
            rng, rollout_count=rollout_count, horizon_months=horizon_months, base_rate_pct=6.5
        )
        private_equity_events = np.zeros((rollout_count, horizon_months + 1), dtype=np.bool_)
        if horizon_months >= 12:
            event_draws = rng.random((rollout_count, horizon_months))
            private_equity_events[:, 1:] = event_draws < (1 / 72)
        _reject_model_config_overshoot(self.model_config, required_locations=required_keys.location_ids)
        home_by_location = _location_factor_map(
            home_base,
            required_locations=required_keys.location_ids,
            annual_adjustment_pct_by_location={
                location_id: params.home_value_annual_adjustment_pct
                for location_id, params in self.model_config.location_params.items()
            },
        )
        rent_by_location = _location_factor_map(
            rent_base,
            required_locations=required_keys.location_ids,
            annual_adjustment_pct_by_location={
                location_id: params.rent_annual_adjustment_pct
                for location_id, params in self.model_config.location_params.items()
            },
        )
        metadata = MarketBundleMetadata(
            market_model_id=market_request.market_model_id,
            scenario_generator_id="simple_market_bundle_provider",
            scenario_generator_version_id="simple_market_bundle_provider:v1",
            evidence_set_id="fixture:simple",
            calibration_artifact_id="fixture:simple",
            risk_factor_ids=CORE_MARKET_RISK_FACTOR_IDS,
            current_private_equity_price_usd=self.current_private_equity_price_usd,
            seed=seed,
            rollout_count=rollout_count,
            horizon_months=horizon_months,
            event_stream_ids=("private_equity_sale_opportunity_event",),
            notes=("simple core stochastic provider; replaceable via MarketBundleProvider",),
        )
        # Placeholder crypto value path: constant 1.0. Until a fitted crypto model
        # plugs in, the simulator carries a flat array so reporting and the
        # crypto sale-funding policy remain valid for any required symbol.
        crypto_value = np.ones((rollout_count, horizon_months + 1), dtype="float64")
        # Required PE issuers and crypto symbols share one placeholder path each
        # under the simple model (per the `private-equity-paths-all-share-placeholder`
        # and `crypto-paths-all-share-placeholder` limitations). If no scenario in
        # the set has those positions, the corresponding dict is legitimately empty.
        pe_value_by_issuer = dict.fromkeys(required_keys.pe_issuer_ids, private_equity_value)
        pe_mask_by_issuer = dict.fromkeys(required_keys.pe_issuer_ids, private_equity_events)
        crypto_by_symbol = dict.fromkeys(required_keys.crypto_symbols, crypto_value)
        return MarketBundle(
            month_index=month_index,
            inflation_multipliers=inflation,
            generic_sp500_multipliers=sp500,
            home_value_multipliers_by_location=home_by_location,
            rent_multipliers_by_location=rent_by_location,
            mortgage_30y_rate_pct=mortgage_rate,
            private_equity_value_multipliers_by_issuer=pe_value_by_issuer,
            private_equity_sale_opportunity_mask_by_issuer=pe_mask_by_issuer,
            crypto_value_multipliers_by_symbol=crypto_by_symbol,
            metadata=metadata,
        )


def _lognormal_multiplier_paths(
    rng: np.random.Generator,
    *,
    rollout_count: int,
    horizon_months: int,
    annual_return_pct: float,
    annual_volatility_pct: float,
) -> np.ndarray:
    monthly_sigma = annual_volatility_pct / 100 / np.sqrt(12)
    monthly_mu = annual_return_pct / 100 / 12 - 0.5 * monthly_sigma**2
    log_returns = rng.normal(monthly_mu, monthly_sigma, size=(rollout_count, horizon_months))
    paths = np.ones((rollout_count, horizon_months + 1), dtype="float64")
    if horizon_months > 0:
        paths[:, 1:] = np.exp(np.cumsum(log_returns, axis=1))
    return paths


def _mortgage_rate_paths(
    rng: np.random.Generator, *, rollout_count: int, horizon_months: int, base_rate_pct: float
) -> np.ndarray:
    monthly_shocks = rng.normal(0.0, 0.08, size=(rollout_count, horizon_months))
    paths = np.full((rollout_count, horizon_months + 1), base_rate_pct, dtype="float64")
    if horizon_months > 0:
        paths[:, 1:] = np.clip(base_rate_pct + np.cumsum(monthly_shocks, axis=1), 0.5, 15.0)
    return paths


def _location_factor_map(
    base: np.ndarray, *, required_locations: frozenset[str], annual_adjustment_pct_by_location: dict[str, float]
) -> dict[str, np.ndarray]:
    """Produce a path dict keyed by `required_locations`. Each location's path is
    `base * (1 + adj/100)^(months/12)`, where `adj` comes from
    `annual_adjustment_pct_by_location[location]` (default 0.0 = unadjusted base).

    `annual_adjustment_pct_by_location` keys must be a subset of `required_locations`
    — see `_reject_model_config_overshoot` for the enforcement at the provider boundary.
    """
    horizon_months = base.shape[1] - 1
    months = np.arange(horizon_months + 1, dtype="float64")
    paths: dict[str, np.ndarray] = {}
    for location in required_locations:
        adjustment_pct = annual_adjustment_pct_by_location.get(location, 0.0)
        if adjustment_pct == 0.0:
            paths[location] = base
        else:
            adjustment = (1 + adjustment_pct / 100) ** (months / 12)
            paths[location] = base * adjustment[None, :]
    return paths


def _reject_model_config_overshoot(
    model_config: SimpleMarketModelConfig, *, required_locations: frozenset[str]
) -> None:
    """A model config that names location ids the scenario set didn't ask for is a
    bug in the deployment's model config (typo, stale id). Fail loud — silently
    populating extra bundle keys would mask the mistake.
    """
    overshoot = sorted(set(model_config.location_params) - required_locations)
    if overshoot:
        raise ValueError(
            f"SimpleMarketModelConfig.location_params has entries for locations "
            f"not declared by the scenario set: {overshoot}. Either remove them from the "
            "model config or add scenarios that reference those locations."
        )
