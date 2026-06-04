"""Runtime configuration for an augur deployment.

The generic augur framework knows nothing about specific users, holdings,
or property shortlists. It loads everything user-specific from a single
validated `Config` at startup. Concretely: `augur.api.server` reads
the path from `AUGUR_CONFIG_PATH` (default `/etc/augur/config.yaml`),
parses + validates via Pydantic, and threads the result through the
backend and frontend bootstrap payload.

This is the contract between the public framework and any user-side
composition layer (e.g. gaffer-private's image-build step that
materializes the user's personal_defaults into a YAML file the
container reads).
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, HttpUrl, PositiveInt, model_validator

from augur.api.local_regulation import LocalRegulation
from augur.api.portfolio_source_config import PortfolioSourcesConfig
from augur.api.schemas import ApiModel
from augur.api.wire import ActorRole, ProductInputDefaults
from augur.budget.schema import BudgetConfig
from augur.model.provider_config import CompositeProviderConfig, MirroringProviderConfig, ProviderConfig
from augur.model.provider_includes import resolve_provider_includes
from augur.model.state_space import StateSpaceProviderConfig
from augur.model.trained_private_equity import TrainedPrivateEquityProviderConfig
from augur.model.vecm import VecmProviderConfig
from augur.product.wire import MAX_HORIZON_MONTHS

AUGUR_CONFIG_PATH_ENV_VAR = "AUGUR_CONFIG_PATH"
DEFAULT_AUGUR_CONFIG_PATH = Path("/etc/augur/config.yaml")


def _duplicates(values) -> list[str]:
    counts = Counter(values)
    return sorted(value for value, count in counts.items() if count > 1)


class AgentDefinition(ApiModel):
    """An economic actor the simulator can attribute state to.

    Actor IDs are user-provided identity strings (e.g. "primary", "buyer").
    The role is a typed concept the policy / scenario engine consumes."""

    actor_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    label: str
    role: ActorRole


class PropertyAssetConfig(ApiModel):
    """Deployment-owned public image URL for one property."""

    property_id: str = Field(min_length=1)
    image_url: HttpUrl


class PropertySourceConfig(ApiModel):
    """Where to find the user's property shortlist + public image URLs.

    `asset_dir` is reserved for deployment-side composers that need local files.
    The generic app does not serve it. Frontend-visible images come from
    `property_assets`, each carrying an absolute image URL.
    """

    properties_path: Path
    asset_dir: Path | None = None
    property_assets: tuple[PropertyAssetConfig, ...] = ()

    @model_validator(mode="after")
    def _validate_property_assets(self) -> PropertySourceConfig:
        duplicate_property_ids = _duplicates(asset.property_id for asset in self.property_assets)
        if duplicate_property_ids:
            raise ValueError(f"duplicate property asset property_ids: {duplicate_property_ids}")
        return self


class CalibrationCatalogConfig(ApiModel):
    """The prediction-market catalog the model-only calibration endpoints score.

    `catalog_path` is a `MarketCatalog` YAML (resolved relative to the config file, like
    `property_source.properties_path`). The catalog self-describes its targets — each PE market
    names its issuer, each macro market its series — so the run covers the union of referenced
    issuers/series (intersected with what the chosen preset can emit)."""

    catalog_path: Path
    label: str | None = None
    sample_sanity_path: Path | None = Field(
        default=None,
        description=(
            "Optional `SampleSanitySpec` YAML (resolved relative to the config file, like "
            "`catalog_path`). When set, `/api/calibration/run` evaluates the spec's reasonableness "
            "bands against the live rollouts and returns them as `sanity_bands`. The spec is consumed "
            "only for its `*_checks` (series/issuers to attempt are derived from each check's key); "
            "its `provider_config_path` is ignored (the page reuses the live calibration model)."
        ),
    )


class LocationConfig(ApiModel):
    """A deployment-owned location identity and its local modeling inputs.

    Built-in locations are available from the public catalog, but fixtures and
    private deployments should define their own IDs here instead of extending
    core enums.
    """

    location_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    label: str
    city: str
    state: str
    local_regulation: LocalRegulation
    notes: tuple[str, ...] = ()


class Config(ApiModel):
    """The single root configuration object an augur deployment reads
    at startup. Everything user-specific lives here.

    `location_selection = None` (the default) means surface the locations
    represented by the loaded property source. A non-None tuple restricts
    the UI / scenarios to that subset.
    """

    agents: tuple[AgentDefinition, ...] = Field(min_length=1)
    property_source: PropertySourceConfig
    portfolio_sources: PortfolioSourcesConfig
    locations: tuple[LocationConfig, ...] = ()
    location_selection: tuple[str, ...] | None = None
    # CLEANUP(2026-06-02): `default_rollout_samples` is no longer surfaced on the wire — the
    #   frontend seeds its rollout count from a local constant clamped to `max_rollout_samples`,
    #   so nothing reads this. Remove the field once gaffer-private's config YAML drops the key
    #   (ApiModel's extra="forbid" rejects unknown keys, so it must outlive deployments still
    #   setting it).
    default_rollout_samples: PositiveInt
    max_rollout_samples: PositiveInt
    max_horizon_months: PositiveInt = Field(
        default=MAX_HORIZON_MONTHS,
        le=MAX_HORIZON_MONTHS,
        description=(
            "Server-side maximum rollout length in months. Every scenario+seed is simulated once to "
            "this horizon (then cached) and the response is truncated to the per-request "
            "`horizon_months`. The frontend's time-axis charts scroll the requested horizon up to "
            "this ceiling, so there is no separate horizon input. Larger values make the first load "
            "of each scenario proportionally heavier; cached truncations are free. Defaults to (and "
            "may not exceed) the wire's absolute cap."
        ),
    )
    # User-overridable starting values for the product input panel. Optional per-field; the
    # frontend layers these over its hard-coded base defaults at bootstrap time so deployments
    # (e.g. `gaffer-private`) can bias the UI without code changes.
    product_input_defaults: ProductInputDefaults = Field(default_factory=ProductInputDefaults)
    models: dict[str, ProviderConfig] = Field(
        min_length=1,
        description=(
            "Named registry of model providers. Frontend exposes the preset id via "
            "`ScenarioKey.model_id` so the user can A/B providers (e.g. a hand-tuned "
            "structured model vs a fitted-artifact-based one). The server materializes each preset "
            "into its own runtime `Sampler` at startup."
        ),
    )
    default_model_id: str = Field(
        description=("Preset id used when the request omits or defaults `model_id`. Must name a key in `models`.")
    )
    calibration_catalog: CalibrationCatalogConfig = Field(
        description=(
            "The single prediction-market catalog the model-only calibration endpoints "
            "(`/api/calibration/*`) score, loaded into a `MarketCatalog` at startup. Every "
            "deployment configures one: a catalog plus the issuer it scores."
        )
    )
    budget: BudgetConfig | None = Field(
        default=None,
        description=(
            "Optional budget planner config. When set, `/api/budget/*` routes are enabled. "
            "Holds bucket taxonomy and per-deployment categorization rules (private merchants "
            "live here, in the deployment's gaffer-private config — not in ducktape)."
        ),
    )

    @model_validator(mode="after")
    def _validate_default_preset(self) -> Config:
        if self.default_model_id not in self.models:
            raise ValueError(
                f"default_model_id {self.default_model_id!r} is not a key in models (have {sorted(self.models)})"
            )
        return self


def load_augur_config(path: Path) -> Config:
    """Parse + validate a Config from a YAML file.

    Before validation, `{provider_config_path: <file>}` provider refs (e.g. the shared macro)
    and a top-level `budget_path` are inlined from their sibling files, so large shared/split
    sections live in one place. Relative deployment-owned file paths are then anchored against
    the yaml's parent directory — useful for ConfigMap mounts where the yaml and adjacent data
    live side-by-side (e.g. `/etc/augur/{config.yaml,properties.json}`)."""
    raw = resolve_provider_includes(yaml.safe_load(path.read_text(encoding="utf-8")), base_dir=path.parent)
    raw = _inline_budget_path(raw, base_dir=path.parent)
    config = Config.model_validate(raw)
    config = _anchor_property_source_paths(config, base_dir=path.parent)
    config = _anchor_model_paths(config, base_dir=path.parent)
    return _anchor_calibration_catalog_paths(config, base_dir=path.parent)


def _inline_budget_path(raw: Any, *, base_dir: Path) -> Any:
    """Inline a top-level `budget_path: <file>` into the `budget` key from its sibling file, so
    the (large, per-deployment) transaction-categorization config can live in its own file."""
    if not isinstance(raw, dict) or "budget_path" not in raw:
        return raw
    if raw.get("budget") is not None:
        raise ValueError("config sets both `budget` and `budget_path`; provide exactly one")
    raw = dict(raw)
    budget_ref = Path(raw.pop("budget_path"))
    budget_path = budget_ref if budget_ref.is_absolute() else base_dir / budget_ref
    raw["budget"] = yaml.safe_load(budget_path.read_text(encoding="utf-8"))
    return raw


def _anchor_calibration_catalog_paths(config: Config, *, base_dir: Path) -> Config:
    catalog = config.calibration_catalog
    updates: dict[str, Path] = {}
    if not catalog.catalog_path.is_absolute():
        updates["catalog_path"] = (base_dir / catalog.catalog_path).resolve()
    if catalog.sample_sanity_path is not None and not catalog.sample_sanity_path.is_absolute():
        updates["sample_sanity_path"] = (base_dir / catalog.sample_sanity_path).resolve()
    if not updates:
        return config
    return config.model_copy(update={"calibration_catalog": catalog.model_copy(update=updates)})


def _anchor_property_source_paths(config: Config, *, base_dir: Path) -> Config:
    source = config.property_source
    properties_path = source.properties_path
    if not properties_path.is_absolute():
        properties_path = (base_dir / properties_path).resolve()
    asset_dir = source.asset_dir
    if asset_dir is not None and not asset_dir.is_absolute():
        asset_dir = (base_dir / asset_dir).resolve()
    if properties_path == source.properties_path and asset_dir == source.asset_dir:
        return config
    return config.model_copy(
        update={
            "property_source": source.model_copy(update={"properties_path": properties_path, "asset_dir": asset_dir})
        }
    )


def _anchor_model_paths(config: Config, *, base_dir: Path) -> Config:
    anchored = {
        preset_id: _anchor_provider_paths(provider, base_dir=base_dir) for preset_id, provider in config.models.items()
    }
    if anchored == dict(config.models):
        return config
    return config.model_copy(update={"models": anchored})


def _anchor_provider_paths(provider: ProviderConfig, *, base_dir: Path) -> ProviderConfig:
    if isinstance(provider, TrainedPrivateEquityProviderConfig):
        trained_model_path = provider.trained_model_path
        if trained_model_path.is_absolute():
            return provider
        return provider.model_copy(update={"trained_model_path": (base_dir / trained_model_path).resolve()})
    if isinstance(provider, VecmProviderConfig):
        trained_blob = provider.trained_blob
        if trained_blob is None or trained_blob.is_absolute():
            return provider
        return provider.model_copy(update={"trained_blob": (base_dir / trained_blob).resolve()})
    if isinstance(provider, StateSpaceProviderConfig):
        trained_artifact_path = provider.trained_artifact_path
        if trained_artifact_path.is_absolute():
            return provider
        return provider.model_copy(update={"trained_artifact_path": (base_dir / trained_artifact_path).resolve()})
    if isinstance(provider, MirroringProviderConfig):
        model = _anchor_provider_paths(provider.model, base_dir=base_dir)
        if model == provider.model:
            return provider
        return provider.model_copy(update={"model": model})
    if isinstance(provider, CompositeProviderConfig):
        macro = _anchor_provider_paths(provider.macro, base_dir=base_dir)
        private_equity = _anchor_provider_paths(provider.private_equity, base_dir=base_dir)
        if macro == provider.macro and private_equity == provider.private_equity:
            return provider
        return provider.model_copy(update={"macro": macro, "private_equity": private_equity})
    return provider


def resolve_augur_config_path() -> Path:
    """Return the path the runtime should read Config from.

    Order of resolution: `$AUGUR_CONFIG_PATH` if set, else
    `/etc/augur/config.yaml` (the conventional k8s ConfigMap mount point)."""
    if env := os.environ.get(AUGUR_CONFIG_PATH_ENV_VAR):
        return Path(env)
    return DEFAULT_AUGUR_CONFIG_PATH


def dump_augur_config_yaml(config: Config) -> str:
    """Serialize a Config to a stable YAML string for ConfigMap mounts.

    Uses Pydantic's JSON-mode dump (so Path/Enum fields serialize cleanly) then
    re-emits as YAML with sorted keys and block style for diff stability."""
    return yaml.safe_dump(
        config.model_dump(mode="json", exclude_computed_fields=True), sort_keys=True, default_flow_style=False
    )
