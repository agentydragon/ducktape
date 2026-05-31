"""Emit the Augur API OpenAPI schema (to stdout) for frontend Zod/TS codegen.

Builds the *real* FastAPI app and dumps its `.openapi()`. The app registers every route —
including `/api/calibration/*` — so the emitted document carries every component schema the
frontend consumes; there is no separate schema-only app to drift. The OpenAPI document is a
function of the route signatures (request/response models), not of any config *data*, so the
exporter constructs a minimal valid deployment in Python (one agent, one location + property,
an empty `independent` exogenous preset) rather than reading a YAML fixture from runfiles —
which is what let it break when ducktape is consumed as an external module.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from augur.api.bootstrap import ActorRole, Property
from augur.api.config import AgentDefinition, Config, LocationConfig, PropertySourceConfig
from augur.api.finance import FinanceSnapshot
from augur.api.local_regulation import LocalRegulation, TaxRegime
from augur.api.server import create_app_from_augur_config
from augur.model.independent import IndependentExogenousProviderConfig

_SCHEMA_LOCATION_ID = "schema_location"

_SCHEMA_PROPERTY = Property(
    id="schema_property",
    source_catalog_id="schema",
    source_property_id="schema-property",
    location_id=_SCHEMA_LOCATION_ID,
    address="Schema Property",
    neighborhood="Schema",
    type="Fixture",
    price_usd=900_000.0,
    rent_estimate_usd=4_200.0,
    beds=3,
    baths=2,
    sqft=1_400,
    year_built=2000,
)


def _schema_export_config(properties_path: Path) -> Config:
    """A minimal valid `Config` sufficient to build the full app for schema export.

    The bootstrap requires a non-empty property catalog whose locations are config-defined, so
    a single location + property are supplied; the exogenous preset is an empty `independent`
    provider (no artifacts). None of this data shapes the OpenAPI document — only the routes do."""

    regulation = LocalRegulation(
        property_tax_regime=TaxRegime.CALIFORNIA_PROP13,
        default_tax_regimes=(TaxRegime.CALIFORNIA_PROP13,),
        property_tax_annual_pct=1.0,
    )
    return Config(
        agents=(AgentDefinition(actor_id="schema", label="Schema", role=ActorRole.PRIMARY_OWNER),),
        property_source=PropertySourceConfig(properties_path=properties_path),
        snapshot=FinanceSnapshot(as_of_date="2026-01-01"),
        default_rollout_samples=1,
        max_rollout_samples=1,
        locations=(
            LocationConfig(
                location_id=_SCHEMA_LOCATION_ID,
                label="Schema",
                city="Schema",
                state="Schema",
                local_regulation=regulation,
            ),
        ),
        exogenous_presets={"schema": IndependentExogenousProviderConfig()},
        default_exogenous_preset_id="schema",
    )


def main() -> None:
    # `PropertySourceConfig` addresses the property catalog by path, so serialize the in-Python
    # property to a temp file. This keeps the exporter free of any checked-in / runfiles fixture.
    with tempfile.TemporaryDirectory() as tmpdir:
        properties_path = Path(tmpdir) / "properties.json"
        properties_path.write_text(json.dumps([_SCHEMA_PROPERTY.model_dump(mode="json")]), encoding="utf-8")
        print(json.dumps(create_app_from_augur_config(_schema_export_config(properties_path)).openapi(), indent=2))


if __name__ == "__main__":
    main()
