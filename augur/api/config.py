"""Runtime configuration for an augur deployment.

The generic augur framework knows nothing about specific users, holdings,
or property shortlists. It loads everything user-specific from a single
validated `AugurConfig` at startup. Concretely: `http_server.py` reads
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

import yaml
from pydantic import Field, HttpUrl, NonNegativeFloat, NonNegativeInt, PositiveInt, model_validator

from augur.core.bootstrap import DefaultScenario
from augur.core.finance import ConcentratedHoldingSnapshot, FinanceSnapshot
from augur.core.local_regulation import LocalRegulation
from augur.core.scenario_set import ActorRole, LiquidityReserveRuleType
from augur.core.schemas import ApiModel

__all__ = [
    "AgentDefinition",
    "AugurConfig",
    "ConcentratedHoldingSnapshot",
    "FinanceSnapshot",
    "LocationConfig",
    "PersonalFinanceConfig",
    "PropertyAssetConfig",
    "PropertySourceConfig",
    "dump_augur_config_yaml",
    "load_augur_config",
]

AUGUR_CONFIG_PATH_ENV_VAR = "AUGUR_CONFIG_PATH"
DEFAULT_AUGUR_CONFIG_PATH = Path("/etc/augur/config.yaml")


def _duplicates(values) -> list[str]:
    counts = Counter(values)
    return sorted(value for value, count in counts.items() if count > 1)


class AgentDefinition(ApiModel):
    """An economic actor the simulator can attribute state to.

    Actor IDs are user-provided identity strings (e.g. "primary", "partner").
    The role is a typed concept the policy / scenario engine consumes."""

    actor_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    label: str
    role: ActorRole


class PersonalFinanceConfig(ApiModel):
    """User-specific finance defaults that are not balance-sheet rows."""

    minimum_liquid_reserve_usd: NonNegativeFloat = 0.0
    default_partner_monthly_payment_usd: NonNegativeFloat = 0.0


class PropertyAssetConfig(ApiModel):
    """Deployment-owned public image address for one property.

    `asset_id` is a stable identity, not a local file path. Deployments may map
    it to any storage backend; the generic app only needs the resulting URL.
    """

    property_id: str = Field(min_length=1)
    asset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    image_url: HttpUrl | None = None


class PropertySourceConfig(ApiModel):
    """Where to find the user's property shortlist + public image URLs.

    `asset_dir` is reserved for deployment-side composers that need local files.
    The generic app does not serve it. Frontend-visible images come from
    `property_assets`: either an explicit `image_url` per asset, or
    `asset_base_url/{asset_id}` when `image_url` is omitted.
    """

    properties_path: Path
    asset_dir: Path | None = None
    asset_base_url: HttpUrl | None = None
    property_assets: tuple[PropertyAssetConfig, ...] = ()

    @model_validator(mode="after")
    def _validate_property_assets(self) -> PropertySourceConfig:
        duplicate_property_ids = _duplicates(asset.property_id for asset in self.property_assets)
        if duplicate_property_ids:
            raise ValueError(f"duplicate property asset property_ids: {duplicate_property_ids}")

        duplicate_asset_ids = _duplicates(asset.asset_id for asset in self.property_assets)
        if duplicate_asset_ids:
            raise ValueError(f"duplicate property asset ids: {duplicate_asset_ids}")

        assets_missing_url = [asset.asset_id for asset in self.property_assets if asset.image_url is None]
        if assets_missing_url and self.asset_base_url is None:
            raise ValueError(
                f"property_assets without image_url require property_source.asset_base_url: {assets_missing_url}"
            )
        return self


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


class AugurConfig(ApiModel):
    """The single root configuration object an augur deployment reads
    at startup. Everything user-specific lives here.

    `location_selection = None` (the default) means surface the locations
    represented by the loaded property source. A non-None tuple restricts
    the UI / scenarios to that subset.
    """

    agents: tuple[AgentDefinition, ...] = Field(min_length=1)
    personal_finance: PersonalFinanceConfig
    property_source: PropertySourceConfig
    snapshot: FinanceSnapshot
    locations: tuple[LocationConfig, ...] = ()
    location_selection: tuple[str, ...] | None = None
    minimum_reserve_mode: LiquidityReserveRuleType = LiquidityReserveRuleType.PROJECTED_DEFICITS
    reserve_forward_months: NonNegativeInt = 12
    starting_portfolio_usd: NonNegativeFloat = 0.0
    pmms_survey_date: str | None = None
    default_rollout_samples: PositiveInt = 128
    bootstrap_default_scenarios: tuple[DefaultScenario, ...] = ()


def load_augur_config(path: Path) -> AugurConfig:
    """Parse + validate an AugurConfig from a YAML file.

    Relative `property_source.properties_path` and `property_source.asset_dir`
    are anchored against the yaml's parent directory — useful for ConfigMap
    mounts where the yaml and the property data live side-by-side (e.g.
    `/etc/augur/{config.yaml,properties.json}`)."""
    config = AugurConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    return _anchor_property_source_paths(config, base_dir=path.parent)


def _anchor_property_source_paths(config: AugurConfig, *, base_dir: Path) -> AugurConfig:
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


def resolve_augur_config_path() -> Path:
    """Return the path the runtime should read AugurConfig from.

    Order of resolution: `$AUGUR_CONFIG_PATH` if set, else
    `/etc/augur/config.yaml` (the conventional k8s ConfigMap mount point)."""
    if env := os.environ.get(AUGUR_CONFIG_PATH_ENV_VAR):
        return Path(env)
    return DEFAULT_AUGUR_CONFIG_PATH


def dump_augur_config_yaml(config: AugurConfig) -> str:
    """Serialize an AugurConfig to a stable YAML string for ConfigMap mounts.

    Uses Pydantic's JSON-mode dump (so Path/Enum fields serialize cleanly) then
    re-emits as YAML with sorted keys and block style for diff stability."""
    return yaml.safe_dump(
        config.model_dump(mode="json", exclude_computed_fields=True), sort_keys=True, default_flow_style=False
    )
