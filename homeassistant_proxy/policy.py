"""Policy evaluation engine for entity access control."""

import asyncio
import contextlib
import logging
import time

from hass_client import HomeAssistantClient
from hass_client.exceptions import AuthenticationFailed, CannotConnect, ConnectionFailed, NotConnected

from homeassistant_proxy.config import Action, EntityInfo, Policy

logger = logging.getLogger(__name__)

_REGISTRY_TTL_SECONDS = 60.0
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 60.0
_BACKOFF_FACTOR = 2.0


class AccessDeniedError(Exception):
    def __init__(self, entity_ids: list[str]):
        self.entity_ids = entity_ids
        super().__init__(f"access denied for entities: {entity_ids}")


class PolicyEnforcer:
    """Manages the entity registry and evaluates entity access policies.

    Maintains a persistent WebSocket connection to HA with automatic
    reconnection. Fetches entity/device registries and caches with TTL.
    Priority: entity_ids > device_ids > area_ids > domains > all.
    """

    def __init__(self, ha_url: str, ha_token: str):
        self._ha_url = ha_url
        self._ha_token = ha_token
        self._entities: dict[str, EntityInfo] | None = None
        self._entities_time: float = 0
        self._client: HomeAssistantClient | None = None
        self._connection_task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._ws_url = ha_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"

    async def start(self) -> None:
        """Start the persistent connection loop."""
        self._connection_task = asyncio.create_task(self._connection_loop())

    async def stop(self) -> None:
        """Stop the connection loop and disconnect."""
        if self._connection_task is not None:
            self._connection_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._connection_task
            self._connection_task = None
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
        self._connected.clear()

    async def _connection_loop(self) -> None:
        """Maintain a persistent WebSocket connection with exponential backoff."""
        backoff = _BACKOFF_INITIAL
        while True:
            try:
                self._client = HomeAssistantClient(self._ws_url, self._ha_token)
                await self._client.connect()
                logger.info("WebSocket connected to Home Assistant")
                backoff = _BACKOFF_INITIAL
                self._connected.set()
                await self._client.start_listening()
            except AuthenticationFailed:
                logger.error("HA authentication failed -- stopping connection loop")
                self._connected.clear()
                return
            except (CannotConnect, ConnectionFailed, NotConnected, OSError) as exc:
                logger.warning(f"HA connection lost: {exc}. Reconnecting in {backoff:.1f}s")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(f"Unexpected error in HA connection loop. Reconnecting in {backoff:.1f}s")
            finally:
                self._connected.clear()
                if self._client is not None:
                    with contextlib.suppress(Exception):
                        await self._client.disconnect()
                    self._client = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX)

    async def _ensure_entities(self) -> dict[str, EntityInfo]:
        now = time.monotonic()
        if self._entities is None or now - self._entities_time >= _REGISTRY_TTL_SECONDS:
            try:
                self._entities = await self._fetch_registry()
                self._entities_time = now
            except (ConnectionError, NotConnected, CannotConnect, ConnectionFailed) as exc:
                if self._entities is not None:
                    logger.warning(f"Registry refresh failed ({exc}), serving stale cache")
                else:
                    raise
        return self._entities

    async def _fetch_registry(self) -> dict[str, EntityInfo]:
        if not self._connected.is_set():
            logger.warning("HA not connected, waiting for connection...")
            try:
                await asyncio.wait_for(self._connected.wait(), timeout=10.0)
            except TimeoutError:
                raise ConnectionError("HA WebSocket not available for registry refresh")

        client = self._client
        assert client is not None

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
