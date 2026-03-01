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


class AccessDeniedError(Exception):
    def __init__(self, entity_ids: list[str]):
        self.entity_ids = entity_ids
        super().__init__(f"access denied for entities: {entity_ids}")


class EntityRegistry:
    """Entity registry with reverse indexes for efficient device/area lookup."""

    def __init__(self, entities: dict[str, EntityInfo]):
        self._entities = entities
        self._by_device: dict[str, list[str]] = {}
        self._by_area: dict[str, list[str]] = {}
        for info in entities.values():
            if info.device_id:
                self._by_device.setdefault(info.device_id, []).append(info.entity_id)
            if info.area_id:
                self._by_area.setdefault(info.area_id, []).append(info.entity_id)

    def get(self, entity_id: str) -> EntityInfo:
        return self._entities.get(entity_id, EntityInfo(entity_id=entity_id))

    def entities_for_devices(self, device_ids: list[str]) -> list[str]:
        return [eid for did in device_ids for eid in self._by_device.get(did, [])]

    def entities_for_areas(self, area_ids: list[str]) -> list[str]:
        return [eid for aid in area_ids for eid in self._by_area.get(aid, [])]


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


class PolicyEnforcer:
    """Evaluates entity access for a token policy against a registry."""

    def __init__(self, policy: Policy, registry: EntityRegistry):
        self._policy = policy
        self._registry = registry

    def is_allowed(self, entity_id: str, action: Action) -> bool:
        return check_entity_access(entity_id, action, self._policy, self._registry.get(entity_id))

    def readable_entities(self, entity_ids: list[str]) -> set[str]:
        return {eid for eid in entity_ids if self.is_allowed(eid, Action.READ)}

    def require_read(self, entity_id: str) -> None:
        if not self.is_allowed(entity_id, Action.READ):
            raise AccessDeniedError([entity_id])

    def require_control(self, entity_ids: list[str]) -> None:
        denied = [eid for eid in entity_ids if not self.is_allowed(eid, Action.CONTROL)]
        if denied:
            raise AccessDeniedError(denied)

    def resolve_targets(self, entity_ids: list[str], device_ids: list[str], area_ids: list[str]) -> list[str]:
        result = list(entity_ids)
        result.extend(self._registry.entities_for_devices(device_ids))
        result.extend(self._registry.entities_for_areas(area_ids))
        return result
