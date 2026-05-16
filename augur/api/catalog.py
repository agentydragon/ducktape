"""Builds the bootstrap payload the augur frontend reads at startup.

Loads the user's property shortlist from `config.property_source.properties_path`
and derives display labels (actor policy, residence mode, rental use) from
`config.agents` so the same generic code serves any deployment's agents."""

from __future__ import annotations

from collections import Counter
from urllib.parse import quote

from pydantic import TypeAdapter

from augur.api.config import AugurConfig, LocationConfig, PropertyAssetConfig
from augur.core.bootstrap import (
    ActorPolicyId,
    ActorPolicyOption,
    AgentOption,
    BootstrapResponse,
    LiquidReservePolicyId,
    LiquidReservePolicyOption,
    Location,
    OwnerResidenceModeId,
    OwnerResidenceModeOption,
    Property,
    RentalUsePolicyId,
    RentalUsePolicyOption,
)
from augur.core.local_regulation import BUILTIN_LOCATION_CONFIGS, BuiltinLocationConfig, local_regulation_for_location
from augur.core.scenario_set import ActorRole
from augur.core.schemas import ScenarioKnobs

PROPERTY_ROWS_ADAPTER = TypeAdapter(tuple[Property, ...])

DEFAULT_KNOBS = ScenarioKnobs(
    down_payment_pct=25,
    credit_score=776,
    custom_mortgage_rate=6.5,
    custom_mortgage_term_years=30,
    starting_portfolio_usd=0,
    hold_years=5,
    appreciation_rate=2,
    sp500_rate=7,
    maintenance_pct=1,
    owner_occupancy_years=0,
    inflation=3,
    vacancy_pct=5,
    mgmt_pct=8,
    leasing_fee_pct=0,
    rooms_rented_while_living=0,
    room_rent_monthly_usd=0,
    room_vacancy_pct=0,
    portfolio_liquidation_tax_pct=0,
    insurance_annual_usd=1800,
    closing_cost_buy_pct=2.5,
    closing_cost_sell_pct=6.5,
    depreciable_basis_pct=80,
    financing_mode="fixed_30",
    occupancy_type="investment",
)


LIQUID_RESERVE_POLICY_OPTIONS = [
    LiquidReservePolicyOption(
        id=LiquidReservePolicyId.NONE,
        label="No automatic sales",
        description="Property cash flows do not trigger portfolio sales in the shared projection.",
    ),
    LiquidReservePolicyOption(
        id=LiquidReservePolicyId.CHECKING_FLOOR_SP500,
        label="Sell SP500 at checking floor",
        description="When checking falls below the floor, sell the configured amount from brokerage.",
    ),
]


def _location_from_config(config: LocationConfig) -> Location:
    return Location(
        id=config.location_id,
        label=config.label,
        city=config.city,
        state=config.state,
        local_regulation=config.local_regulation,
        notes=config.notes,
    )


def _location_from_builtin_config(config: BuiltinLocationConfig) -> Location:
    return Location(
        id=config.location_id,
        label=config.label,
        city=config.city,
        state=config.state,
        local_regulation=local_regulation_for_location(config.location_id),
        notes=config.notes,
    )


def _locations_for_config(config: AugurConfig) -> tuple[Location, ...]:
    locations = tuple(_location_from_config(location) for location in config.locations) + tuple(
        _location_from_builtin_config(location) for location in BUILTIN_LOCATION_CONFIGS
    )
    location_id_counts = Counter(location.id for location in locations)
    duplicate_ids = sorted(location_id for location_id, count in location_id_counts.items() if count > 1)
    if duplicate_ids:
        raise ValueError(f"Augur location catalog has duplicate location ids: {duplicate_ids}")
    return locations


def _validate_property_location(property_: Property, *, location_by_id: dict[str, Location]) -> None:
    if property_.location_id not in location_by_id:
        raise ValueError(f"property {property_.id!r} references unknown location {property_.location_id!r}")


def _public_image_url(asset: PropertyAssetConfig, *, config: AugurConfig) -> str:
    if asset.image_url is not None:
        return str(asset.image_url)
    asset_base_url = config.property_source.asset_base_url
    if asset_base_url is None:
        raise ValueError(f"property asset {asset.asset_id!r} has no image_url or asset_base_url")
    return f"{str(asset_base_url).rstrip('/')}/{quote(asset.asset_id, safe='')}"


def _apply_property_assets(config: AugurConfig, properties: tuple[Property, ...]) -> tuple[Property, ...]:
    property_assets = config.property_source.property_assets
    if not property_assets:
        return properties

    property_ids = {property_.id for property_ in properties}
    unknown_property_ids = sorted(
        asset.property_id for asset in property_assets if asset.property_id not in property_ids
    )
    if unknown_property_ids:
        raise ValueError(f"property_assets reference unknown property ids: {unknown_property_ids}")

    image_url_by_property_id = {asset.property_id: _public_image_url(asset, config=config) for asset in property_assets}
    return tuple(
        property_.model_copy(update={"image_url": image_url_by_property_id.get(property_.id, property_.image_url)})
        for property_ in properties
    )


def _default_knobs_for_config(config: AugurConfig) -> ScenarioKnobs:
    starting_portfolio_usd = config.starting_portfolio_usd or config.snapshot.sp500_proxy_portfolio_usd
    return DEFAULT_KNOBS.model_copy(update={"starting_portfolio_usd": starting_portfolio_usd})


def _agents_by_role(config: AugurConfig) -> tuple[str, str | None]:
    """Return (primary_label, partner_label_or_none) derived from config.agents."""
    primary = next(agent for agent in config.agents if agent.role is ActorRole.PRIMARY_OWNER)
    partner = next((agent for agent in config.agents if agent.role is ActorRole.EQUITY_BUILDING_OCCUPANT), None)
    return primary.label, partner.label if partner is not None else None


def _actor_policy_options(primary: str, partner: str | None) -> list[ActorPolicyOption]:
    options = [
        ActorPolicyOption(
            id=ActorPolicyId.OWNER_ONLY,
            label=f"{primary} only",
            description=f"{primary} funds the purchase and owns the property economics.",
        )
    ]
    if partner is not None:
        options.append(
            ActorPolicyOption(
                id=ActorPolicyId.OWNER_PLUS_PARTNER,
                label=f"{primary} + {partner}",
                description=(
                    f"{partner} contributes while housed and earns proportional equity through the shared actor policy."
                ),
            )
        )
    return options


def _owner_residence_mode_options(primary: str) -> list[OwnerResidenceModeOption]:
    return [
        OwnerResidenceModeOption(
            id=OwnerResidenceModeId.SELECTED_PROPERTY,
            label=f"{primary} lives in selected property",
            description=f"{primary} occupies the selected property for the modeled owner-occupancy period.",
        ),
        OwnerResidenceModeOption(
            id=OwnerResidenceModeId.OTHER_OWNED_PROPERTY,
            label=f"{primary} lives in another modeled property",
            description=(
                f"{primary}'s residence is another selected property while this property can be rented or held."
            ),
        ),
        OwnerResidenceModeOption(
            id=OwnerResidenceModeId.RENTAL_ELSEWHERE,
            label=f"{primary} rents elsewhere",
            description=(f"{primary} does not live in a modeled owned property in this scenario."),
        ),
    ]


def _rental_use_policy_options(primary: str, partner: str | None) -> list[RentalUsePolicyOption]:
    occupant_phrase = f"{primary} or {partner}" if partner is not None else primary
    return [
        RentalUsePolicyOption(
            id=RentalUsePolicyId.NOT_RENTED,
            label="Not rented",
            description=f"No rental income is modeled while {occupant_phrase} uses the property.",
        ),
        RentalUsePolicyOption(
            id=RentalUsePolicyId.RENT_ROOMS_WHILE_OWNER_LIVES_THERE,
            label="Rent rooms while living there",
            description=f"Room rental income applies during {primary}'s owner-occupancy period.",
        ),
        RentalUsePolicyOption(
            id=RentalUsePolicyId.RENT_WHOLE_PROPERTY,
            label="Rent whole property",
            description=(
                f"Whole-property rental income applies when the property is not occupied by {occupant_phrase}."
            ),
        ),
    ]


def _load_properties(config: AugurConfig, *, location_by_id: dict[str, Location]) -> tuple[Property, ...]:
    path = config.property_source.properties_path
    properties = PROPERTY_ROWS_ADAPTER.validate_json(path.read_text(encoding="utf-8"))
    for property_ in properties:
        _validate_property_location(property_, location_by_id=location_by_id)
    property_id_counts = Counter(property_.id for property_ in properties)
    duplicate_ids = sorted(property_id for property_id, count in property_id_counts.items() if count > 1)
    if duplicate_ids:
        raise ValueError(f"{path} has duplicate property ids: {duplicate_ids}")
    return _apply_property_assets(config, properties)


def load_properties(config: AugurConfig) -> tuple[Property, ...]:
    locations = _locations_for_config(config)
    return _load_properties(config, location_by_id={location.id: location for location in locations})


def build_bootstrap_payload(config: AugurConfig) -> BootstrapResponse:
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
    primary, partner = _agents_by_role(config)
    return BootstrapResponse(
        locations=locations,
        properties=properties,
        default_property_id=properties[0].id,
        default_actor_policy=ActorPolicyId.OWNER_ONLY,
        default_owner_residence_mode=OwnerResidenceModeId.SELECTED_PROPERTY,
        default_owner_residence_property_id=properties[0].id,
        default_rental_use_policy=RentalUsePolicyId.NOT_RENTED,
        default_liquid_reserve_policy=LiquidReservePolicyId.NONE,
        default_initial_checking_usd=config.snapshot.cash_usd,
        default_checking_floor_usd=10_000,
        default_checking_sale_amount_usd=20_000,
        default_knobs=_default_knobs_for_config(config),
        default_rollout_samples=config.default_rollout_samples,
        default_scenarios=list(config.bootstrap_default_scenarios),
        actor_policy_options=_actor_policy_options(primary, partner),
        owner_residence_mode_options=_owner_residence_mode_options(primary),
        rental_use_policy_options=_rental_use_policy_options(primary, partner),
        liquid_reserve_policy_options=LIQUID_RESERVE_POLICY_OPTIONS,
        agents=[AgentOption(actor_id=agent.actor_id, label=agent.label, role=agent.role) for agent in config.agents],
        finance_snapshot=config.snapshot,
        default_partner_monthly_payment_usd=config.personal_finance.default_partner_monthly_payment_usd,
    )
