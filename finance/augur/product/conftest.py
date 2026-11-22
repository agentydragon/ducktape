from __future__ import annotations

from collections.abc import Callable

import pytest

from finance.augur.api.catalog import build_catalog
from finance.augur.api.config import Config
from finance.augur.api.product_service import build_product_service
from finance.augur.api.wire import CatalogResponse
from finance.augur.model.exogenous import Sampler
from finance.augur.product.service import ProductService
from finance.augur.x.models.independent import IndependentProviderConfig
from finance.augur.x.models.provider_config import CompositeProviderConfig, MirroringProviderConfig, ProviderConfig

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
        cat = catalog if config is None else None
        return build_product_service(cfg, {"current_model": model}, catalog=cat)

    return _make


def with_fixture_macro(
    provider: ProviderConfig, update: Callable[[IndependentProviderConfig], IndependentProviderConfig]
) -> CompositeProviderConfig:
    """Edit a fixture preset's independent series while retaining its mirrors and PE provider."""
    assert isinstance(provider, CompositeProviderConfig)
    macro = provider.macro
    assert isinstance(macro, MirroringProviderConfig)
    assert isinstance(macro.model, IndependentProviderConfig)
    return provider.model_copy(update={"macro": macro.model_copy(update={"model": update(macro.model)})})
