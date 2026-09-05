"""Build the product service from one validated Augur deployment."""

from __future__ import annotations

from finance.augur.api.catalog import build_catalog
from finance.augur.api.config import Config
from finance.augur.api.portfolio_sources import resolve_portfolio_sources
from finance.augur.api.wire import CatalogResponse
from finance.augur.model.exogenous import Sampler
from finance.augur.product.scenarios import resolve_primary_agent_id, sim_locations_from_config
from finance.augur.product.service import ProductService, SummaryBackend


def build_product_service(
    config: Config,
    models: dict[str, Sampler],
    *,
    catalog: CatalogResponse | None = None,
    summary_backend: SummaryBackend = SummaryBackend.JAX,
) -> ProductService:
    """Build a product service from deployment config and realized model presets.

    ``catalog`` is injectable for callers that already built the deployment catalog (notably
    the API server and hermetic tests). When config is modified, omitting it rebuilds the
    catalog so location/property validation follows the modified deployment.

    ``summary_backend`` selects which simulator answers the projection endpoints; it is an
    explicit opt-in so a deployment can run the Rust engine beside JAX and compare.
    """

    resolved_portfolio = resolve_portfolio_sources(config)
    catalog = build_catalog(config) if catalog is None else catalog
    return ProductService(
        portfolio=resolved_portfolio.portfolio,
        initial_cash=resolved_portfolio.snapshot.cash,
        primary_agent_id=resolve_primary_agent_id(config),
        security_distributions=config.security_distributions,
        harvest_policies=resolved_portfolio.harvest_policies,
        known_location_ids=catalog.location_ids,
        locations=sim_locations_from_config(config.locations),
        properties_by_id=catalog.properties_by_id,
        models=models,
        max_rollout_samples=config.max_rollout_samples,
        max_horizon_months=config.max_horizon_months,
        summary_backend=summary_backend,
    )
