"""Policy evaluation engine for entity access control."""

import logging
import time

from hass_client import HomeAssistantClient

from homeassistant_proxy.config import Action, EntityInfo, Policy

logger = logging.getLogger(__name__)

_REGISTRY_TTL_SECONDS = 60.0


class AccessDeniedError(Exception):
    def __init__(self, entity_ids: list[str]):
        self.entity_ids = entity_ids
        super().__init__(f"access denied for entities: {entity_ids}")


class PolicyEnforcer:
    """Manages the entity registry and evaluates entity access policies.

    Owns registry lifecycle: fetches from HA via WebSocket, caches with TTL,
    refreshes automatically.
    Priority: entity_ids > device_ids > area_ids > domains > all.
    """

    def __init__(
        self,
        ha_url: str,
        ha_token: str,
        *,
        ttl: float = _REGISTRY_TTL_SECONDS,
        entities: dict[str, EntityInfo] | None = None,
    ):
        self._ha_url = ha_url
        self._ha_token = ha_token
        self._ttl = ttl
        self._entities: dict[str, EntityInfo] | None = entities
        self._entities_time: float = 0

    async def _ensure_entities(self) -> dict[str, EntityInfo]:
        now = time.monotonic()
        if self._entities is None or now - self._entities_time >= self._ttl:
            self._entities = await self._fetch_registry()
            self._entities_time = now
        return self._entities

    async def _fetch_registry(self) -> dict[str, EntityInfo]:
        ws_url = self._ha_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/api/websocket"

        async with HomeAssistantClient(ws_url, self._ha_token) as client:
            entities = await client.get_entity_registry()
            devices = await client.get_device_registry()

        device_area: dict[str, str | None] = {d["id"]: d["area_id"] for d in devices}

        registry: dict[str, EntityInfo] = {}
        for entity in entities:
            entity_id = entity["entity_id"]
            device_id = entity["device_id"]
            area_id = entity["area_id"]
            if not area_id and device_id:
                area_id = device_area.get(device_id)
            registry[entity_id] = EntityInfo(entity_id=entity_id, device_id=device_id, area_id=area_id)

        logger.info(f"Fetched registry: {len(registry)} entities")
        return registry

    def _get_entity(self, entities: dict[str, EntityInfo], entity_id: str) -> EntityInfo:
        return entities.get(entity_id, EntityInfo(entity_id=entity_id))

    def _entities_for_devices(self, entities: dict[str, EntityInfo], device_ids: list[str]) -> list[str]:
        ids = set(device_ids)
        return [info.entity_id for info in entities.values() if info.device_id in ids]

    def _entities_for_areas(self, entities: dict[str, EntityInfo], area_ids: list[str]) -> list[str]:
        ids = set(area_ids)
        return [info.entity_id for info in entities.values() if info.area_id in ids]

    async def is_allowed(self, entity_id: str, action: Action, policy: Policy) -> bool:
        entities = await self._ensure_entities()
        info = self._get_entity(entities, entity_id)
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
        entities = await self._ensure_entities()
        result = list(entity_ids)
        result.extend(self._entities_for_devices(entities, device_ids))
        result.extend(self._entities_for_areas(entities, area_ids))
        return result
