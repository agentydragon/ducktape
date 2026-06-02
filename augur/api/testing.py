"""Shared test fixtures/builders for the public augur deployment fixture.

Test-only helpers reused across `augur/api` and `augur/product` test modules and the visual
test: the public testdata `Config`, a minimal placeholder `Config` for schema-shape tests, and
the synthetic `LocalRegulation`s the property/location fixtures are built from."""

from __future__ import annotations

from augur.api.config import AgentDefinition, Config, PropertySourceConfig, load_augur_config
from augur.api.finance import FinanceSnapshot
from augur.api.local_regulation import LocalRegulation, TaxRegime
from augur.api.portfolio_source_config import FixedPortfolioSourceConfig, PortfolioSourcesConfig
from augur.api.wire import ActorRole
from augur.model.independent import IndependentProviderConfig
from util.bazel.runfiles import get_required_path


def load_fixture_config() -> Config:
    """The public testdata deployment config (`augur/api/testdata/config.yaml`)."""
    return load_augur_config(get_required_path("_main/augur/api/testdata/config.yaml"))


def minimal_config(**overrides: object) -> Config:
    """A minimal valid single-owner `Config` for schema-shape tests; deployments supply real
    values. Tests override only the fields they assert on."""
    defaults: dict[str, object] = {
        "agents": (AgentDefinition(actor_id="owner", label="Owner", role=ActorRole.PRIMARY_OWNER),),
        "property_source": PropertySourceConfig(properties_path="/tmp/properties.json"),
        "portfolio_sources": PortfolioSourcesConfig(
            fixed=FixedPortfolioSourceConfig(snapshot=FinanceSnapshot(as_of_date="2026-05-12"))
        ),
        "default_rollout_samples": 128,
        "max_rollout_samples": 1_000_000,
        "models": {"current_model": IndependentProviderConfig()},
        "default_model_id": "current_model",
    }
    defaults.update(overrides)
    return Config(**defaults)


def fixture_regulation() -> LocalRegulation:
    """Synthetic CALIFORNIA_PROP13 regulation for the public-fixture locations."""
    return LocalRegulation(
        property_tax_regime=TaxRegime.CALIFORNIA_PROP13,
        default_tax_regimes=(
            TaxRegime.CALIFORNIA_PROP13,
            TaxRegime.CALIFORNIA_TRANSFER_TAX,
            TaxRegime.FEDERAL_MORTGAGE_INTEREST,
            TaxRegime.FEDERAL_CAPITAL_GAINS,
            TaxRegime.CALIFORNIA_INCOME_TAX,
        ),
        property_tax_annual_pct=1.0,
        notes="Synthetic public fixture location.",
    )


def san_francisco_regulation() -> LocalRegulation:
    """San Francisco secured-property-tax regulation (the real SF regime stack)."""
    return LocalRegulation(
        property_tax_regime=TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX,
        default_tax_regimes=(
            TaxRegime.CALIFORNIA_PROP13,
            TaxRegime.CALIFORNIA_TRANSFER_TAX,
            TaxRegime.FEDERAL_MORTGAGE_INTEREST,
            TaxRegime.FEDERAL_CAPITAL_GAINS,
            TaxRegime.CALIFORNIA_INCOME_TAX,
            TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX,
            TaxRegime.SAN_FRANCISCO_TRANSFER_TAX,
        ),
        property_tax_annual_pct=1.18,
        notes="San Francisco fixture",
    )
