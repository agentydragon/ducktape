"""Emit the Augur API OpenAPI schema (to stdout) for frontend Zod/TS codegen.

Builds the *real* FastAPI app and dumps its `.openapi()`. The app registers every route —
including `/api/calibration/*` — so the emitted document carries every component schema the
frontend consumes; there is no separate schema-only app to drift. The OpenAPI document is a
function of the route signatures (request/response models), not of any config *data*, so the
exporter constructs a minimal valid deployment in Python (one agent, one location + property,
an empty `independent` model preset) rather than reading a YAML fixture from runfiles —
which is what let it break when ducktape is consumed as an external module.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from finance.augur.api.config import (
    AgentDefinition,
    CalibrationCatalogConfig,
    Config,
    LocationConfig,
    PropertySourceConfig,
)
from finance.augur.api.finance import FinanceSnapshot
from finance.augur.api.local_regulation import LocalRegulation, TaxRegime
from finance.augur.api.portfolio_source_config import FixedPortfolioSourceConfig, PortfolioSourcesConfig
from finance.augur.api.server import create_app_from_augur_config, static_price_clients
from finance.augur.api.wire import ActorRole, Property
from finance.augur.model.independent import IndependentProviderConfig

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


def _schema_export_config(properties_path: Path, calibration_catalog_path: Path) -> Config:
    """A minimal valid `Config` sufficient to build the full app for schema export.

    The bootstrap requires a non-empty property catalog whose locations are config-defined, so
    a single location + property are supplied; the model preset is an empty `independent`
    provider (no artifacts), and the calibration catalog points at an empty `MarketCatalog`.
    None of this data shapes the OpenAPI document — only the routes do."""

    regulation = LocalRegulation(
        property_tax_regime=TaxRegime.CALIFORNIA_PROP13,
        default_tax_regimes=(TaxRegime.CALIFORNIA_PROP13,),
        property_tax_annual_pct=1.0,
    )
    return Config(
        agents=(AgentDefinition(actor_id="schema", label="Schema", role=ActorRole.PRIMARY_OWNER),),
        property_source=PropertySourceConfig(properties_path=properties_path),
        portfolio_sources=PortfolioSourcesConfig(
            fixed=FixedPortfolioSourceConfig(snapshot=FinanceSnapshot(as_of_date="2026-01-01"))
        ),
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
        models={"schema": IndependentProviderConfig()},
        default_model_id="schema",
        calibration_catalog=CalibrationCatalogConfig(catalog_path=calibration_catalog_path),
    )


def main() -> None:
    # `PropertySourceConfig` addresses the property catalog by path, so serialize the in-Python
    # property to a temp file. This keeps the exporter free of any checked-in / runfiles fixture.
    with tempfile.TemporaryDirectory() as tmpdir:
        properties_path = Path(tmpdir) / "properties.json"
        properties_path.write_text(json.dumps([_SCHEMA_PROPERTY.model_dump(mode="json")]), encoding="utf-8")
        # `create_app_from_augur_config` loads the calibration catalog at startup, so materialize a
        # minimal empty `MarketCatalog` (only its routes/component schemas matter for the export).
        calibration_catalog_path = Path(tmpdir) / "calibration_catalog.yaml"
        calibration_catalog_path.write_text('metadata: {as_of: "2026-01-01"}\nmarkets: []\n', encoding="utf-8")
        print(
            json.dumps(
                create_app_from_augur_config(
                    _schema_export_config(properties_path, calibration_catalog_path),
                    price_clients=static_price_clients({}),
                ).openapi(),
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
