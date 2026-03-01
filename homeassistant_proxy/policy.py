"""Policy evaluation engine for entity access control."""

from pydantic import BaseModel

from homeassistant_proxy.config import Action, Policy


class EntityInfo(BaseModel):
    """Registry-resolved metadata for an entity."""

    entity_id: str
    device_id: str | None = None
    area_id: str | None = None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".")[0]


def check_entity_access(entity_id: str, action: Action, policy: Policy, entity_info: EntityInfo) -> bool:
    """Check whether a policy allows an action on an entity.

    Priority: entity_ids > device_ids > area_ids > domains > all.
    """
    if entity_id in policy.entity_ids:
        return policy.entity_ids[entity_id].allows(action)
    if entity_info.device_id and entity_info.device_id in policy.device_ids:
        return policy.device_ids[entity_info.device_id].allows(action)
    if entity_info.area_id and entity_info.area_id in policy.area_ids:
        return policy.area_ids[entity_info.area_id].allows(action)
    domain = entity_info.domain
    if domain in policy.domains:
        return policy.domains[domain].allows(action)
    return policy.all.allows(action)


def allowed_entities(
    entity_ids: list[str], action: Action, policy: Policy, registry: dict[str, EntityInfo]
) -> list[str]:
    """Return entity IDs that the policy allows for the given action."""
    return [
        eid
        for eid in entity_ids
        if check_entity_access(eid, action, policy, registry.get(eid, EntityInfo(entity_id=eid)))
    ]


def denied_entities(
    entity_ids: list[str], action: Action, policy: Policy, registry: dict[str, EntityInfo]
) -> list[str]:
    """Return entity IDs that the policy DENIES for the given action."""
    return [
        eid
        for eid in entity_ids
        if not check_entity_access(eid, action, policy, registry.get(eid, EntityInfo(entity_id=eid)))
    ]
