"""Builds the bootstrap payload the augur frontend reads at startup.

Loads the user's property shortlist from `config.property_source.properties_path`
and derives display labels (residence mode, rental use) from `config.agents`
so the same generic code serves any deployment's agents."""

from __future__ import annotations

from collections import Counter

import yaml
from more_itertools import one
from pydantic import TypeAdapter

from augur.api.bootstrap import ActorRole, BootstrapResponse, CalibrationInfo, Location, Property
from augur.api.config import Config, LocationConfig, PropertyAssetConfig
from augur.product.wire import MAX_HORIZON_MONTHS

PROPERTY_ROWS_ADAPTER = TypeAdapter(tuple[Property, ...])


def _location_from_config(config: LocationConfig) -> Location:
    return Location(
        id=config.location_id,
        label=config.label,
        city=config.city,
        state=config.state,
        local_regulation=config.local_regulation,
        notes=config.notes,
    )


def _locations_for_config(config: Config) -> tuple[Location, ...]:
    locations = tuple(_location_from_config(location) for location in config.locations)
    location_id_counts = Counter(location.id for location in locations)
    duplicate_ids = sorted(location_id for location_id, count in location_id_counts.items() if count > 1)
    if duplicate_ids:
        raise ValueError(f"Augur location catalog has duplicate location ids: {duplicate_ids}")
    return locations


def _validate_property_location(property_: Property, *, location_by_id: dict[str, Location]) -> None:
    if property_.location_id not in location_by_id:
        raise ValueError(f"property {property_.id!r} references unknown location {property_.location_id!r}")


def _public_image_url(asset: PropertyAssetConfig) -> str:
    return str(asset.image_url)


def _apply_property_assets(config: Config, properties: tuple[Property, ...]) -> tuple[Property, ...]:
    property_assets = config.property_source.property_assets
    if not property_assets:
        return properties

    property_ids = {property_.id for property_ in properties}
    unknown_property_ids = sorted(
        asset.property_id for asset in property_assets if asset.property_id not in property_ids
    )
    if unknown_property_ids:
        raise ValueError(f"property_assets reference unknown property ids: {unknown_property_ids}")

    image_url_by_property_id = {asset.property_id: _public_image_url(asset) for asset in property_assets}
    return tuple(
        property_.model_copy(update={"image_url": image_url_by_property_id.get(property_.id, property_.image_url)})
        for property_ in properties
    )


def _validate_primary_agent_exists(config: Config) -> None:
    """`config.agents` must include exactly one PRIMARY_OWNER. The product surface assumes a
    single primary throughout, so reject early on misconfig rather than failing deep in the
    sim translator."""

    one(agent for agent in config.agents if agent.role is ActorRole.PRIMARY_OWNER)


def _load_properties(config: Config, *, location_by_id: dict[str, Location]) -> tuple[Property, ...]:
    path = config.property_source.properties_path
    # `yaml.safe_load` reads both YAML and JSON (JSON is a YAML subset), so either
    # extension is supported; deployments pick whichever is more ergonomic.
    properties = PROPERTY_ROWS_ADAPTER.validate_python(yaml.safe_load(path.read_text(encoding="utf-8")))
    for property_ in properties:
        _validate_property_location(property_, location_by_id=location_by_id)
    property_id_counts = Counter(property_.id for property_ in properties)
    duplicate_ids = sorted(property_id for property_id, count in property_id_counts.items() if count > 1)
    if duplicate_ids:
        raise ValueError(f"{path} has duplicate property ids: {duplicate_ids}")
    return _apply_property_assets(config, properties)


def _calibration_info(config: Config) -> CalibrationInfo | None:
    catalog = config.calibration_catalog
    if catalog is None:
        return None
    return CalibrationInfo(label=catalog.label or catalog.issuer, issuer=catalog.issuer)


def build_bootstrap_payload(config: Config) -> BootstrapResponse:
    available_locations = _locations_for_config(config)
    location_by_id = {location.id: location for location in available_locations}
    loaded_properties = _load_properties(config, location_by_id=location_by_id)
    selected_location_ids = (
        set(config.location_selection)
        if config.location_selection is not None
        else {property_.location_id for property_ in loaded_properties}
    )
    unknown_selected_locations = sorted(selected_location_ids - set(location_by_id))
    if unknown_selected_locations:
        raise ValueError(f"location_selection references unknown location ids: {unknown_selected_locations}")
    locations = [location for location in available_locations if location.id in selected_location_ids]
    properties = sorted(
        (property_ for property_ in loaded_properties if property_.location_id in selected_location_ids),
        key=lambda property_: (location_by_id[property_.location_id].city, property_.price_usd, property_.id),
    )
    if not properties:
        raise ValueError("Augur property catalog has no properties after applying location_selection")
    _validate_primary_agent_exists(config)
    return BootstrapResponse(
        locations=locations,
        properties=properties,
        default_rollout_samples=config.default_rollout_samples,
        max_rollout_samples=config.max_rollout_samples,
        max_horizon_months=MAX_HORIZON_MONTHS,
        product_input_defaults=config.product_input_defaults,
        exogenous_presets=tuple(sorted(config.exogenous_presets)),
        default_exogenous_preset_id=config.default_exogenous_preset_id,
        calibration=_calibration_info(config),
    )
