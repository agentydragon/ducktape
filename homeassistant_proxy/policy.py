"""Policy evaluation engine for entity access control."""

import logging
import time

from homeassistant_proxy.config import Action, EntityInfo, Policy
from homeassistant_proxy.registry import fetch_registry

logger = logging.getLogger(__name__)

_REGISTRY_TTL_SECONDS = 60.0


class AccessDeniedError(Exception):
    def __init__(self, entity_ids: list[str]):
        self.entity_ids = entity_ids
        super().__init__(f"access denied for entities: {entity_ids}")


class EntityRegistry:
    """Entity registry for looking up entity metadata by ID, device, or area."""

    def __init__(self, entities: dict[str, EntityInfo]):
        self._entities = entities

    def get(self, entity_id: str) -> EntityInfo:
        return self._entities.get(entity_id, EntityInfo(entity_id=entity_id))

    def entities_for_devices(self, device_ids: list[str]) -> list[str]:
        ids = set(device_ids)
        return [info.entity_id for info in self._entities.values() if info.device_id in ids]

    def entities_for_areas(self, area_ids: list[str]) -> list[str]:
        ids = set(area_ids)
        return [info.entity_id for info in self._entities.values() if info.area_id in ids]


class PolicyEnforcer:
    """Manages the entity registry and evaluates entity access policies.

    Owns registry lifecycle: fetches from HA, caches with TTL, refreshes automatically.
    Priority: entity_ids > device_ids > area_ids > domains > all.
    """

    def __init__(
        self, ha_url: str, ha_token: str, *, ttl: float = _REGISTRY_TTL_SECONDS, registry: EntityRegistry | None = None
    ):
        self._ha_url = ha_url
        self._ha_token = ha_token
        self._ttl = ttl
        self._registry = registry
        self._registry_time: float = 0

    async def _ensure_registry(self) -> EntityRegistry:
        now = time.monotonic()
        if self._registry is None or now - self._registry_time >= self._ttl:
            raw = await fetch_registry(self._ha_url, self._ha_token)
            self._registry = EntityRegistry(raw)
            self._registry_time = now
        return self._registry

    async def is_allowed(self, entity_id: str, action: Action, policy: Policy) -> bool:
        registry = await self._ensure_registry()
        info = registry.get(entity_id)
        if entity_id in policy.entity_ids:
            return policy.entity_ids[entity_id].allows(action)
        if info.device_id and info.device_id in policy.device_ids:
            return policy.device_ids[info.device_id].allows(action)
        if info.area_id and info.area_id in policy.area_ids:
            return policy.area_ids[info.area_id].allows(action)
        domain = info.domain
        if domain in policy.domains:
            return policy.domains[domain].allows(action)
        return policy.all.allows(action)

    async def readable_entities(self, entity_ids: list[str], policy: Policy) -> set[str]:
        return {eid for eid in entity_ids if await self.is_allowed(eid, Action.READ, policy)}

    async def require_read(self, entity_id: str, policy: Policy) -> None:
        if not await self.is_allowed(entity_id, Action.READ, policy):
            raise AccessDeniedError([entity_id])

    async def require_control(self, entity_ids: list[str], policy: Policy) -> None:
        denied = [eid for eid in entity_ids if not await self.is_allowed(eid, Action.CONTROL, policy)]
        if denied:
            raise AccessDeniedError(denied)

    async def resolve_targets(self, entity_ids: list[str], device_ids: list[str], area_ids: list[str]) -> list[str]:
        registry = await self._ensure_registry()
        result = list(entity_ids)
        result.extend(registry.entities_for_devices(device_ids))
        result.extend(registry.entities_for_areas(area_ids))
        return result
