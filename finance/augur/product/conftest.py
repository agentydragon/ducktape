from __future__ import annotations

from collections.abc import Callable

import pytest

from finance.augur.api.catalog import build_catalog
from finance.augur.api.config import Config
from finance.augur.api.portfolio_sources import resolve_portfolio_sources
from finance.augur.api.wire import CatalogResponse
from finance.augur.model.exogenous import Sampler
from finance.augur.product.scenarios import resolve_primary_agent_id, sim_locations_from_config
from finance.augur.product.service import ProductService

# What the `make_product_service` fixture hands tests: build a ProductService for one model.
MakeProductService = Callable[..., ProductService]


@pytest.fixture(scope="module")
def catalog(augur_config: Config) -> CatalogResponse:
    return build_catalog(augur_config)


@pytest.fixture
def make_product_service(augur_config: Config, catalog: CatalogResponse) -> MakeProductService:
    """Factory building a ProductService for one exogenous `model` over the fixture deployment.

    Pass `config=` to run against a modified deployment (e.g. `_with_fixed_cash`); the catalog is
    rebuilt from it, otherwise the shared `catalog` fixture is reused."""

    def _make(model: Sampler, *, config: Config | None = None) -> ProductService:
        cfg = augur_config if config is None else config
        cat = catalog if config is None else build_catalog(config)
        resolved = resolve_portfolio_sources(cfg)
        return ProductService(
            portfolio=resolved.portfolio,
            initial_cash_usd=float(resolved.snapshot.cash_usd),
            primary_agent_id=resolve_primary_agent_id(cfg),
            security_distributions=cfg.security_distributions,
            harvest_policies=resolved.harvest_policies,
            known_location_ids=cat.location_ids,
            locations=sim_locations_from_config(cfg.locations),
            properties_by_id=cat.properties_by_id,
            models={"current_model": model},
            max_rollout_samples=cfg.max_rollout_samples,
            max_horizon_months=cfg.max_horizon_months,
        )

    return _make
