"""Fetch entity/device registries from Home Assistant via the hass-client library."""

import logging

from hass_client import HomeAssistantClient

from homeassistant_proxy.policy import EntityInfo

logger = logging.getLogger(__name__)


async def fetch_registry(ha_url: str, ha_token: str) -> dict[str, EntityInfo]:
    """Connect to HA WebSocket, fetch registries, return entity_id -> EntityInfo mapping."""
    ws_url = ha_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url}/api/websocket"

    async with HomeAssistantClient(ws_url, ha_token) as client:
        entities = await client.get_entity_registry()
        devices = await client.get_device_registry()

    # Build device_id -> area_id mapping from device registry
    device_area: dict[str, str | None] = {d["id"]: d["area_id"] for d in devices}

    # Build entity_id -> EntityInfo mapping
    registry: dict[str, EntityInfo] = {}
    for entity in entities:
        entity_id = entity["entity_id"]
        device_id = entity["device_id"]
        # Entity's own area_id takes precedence; fall back to device's area_id
        area_id = entity["area_id"]
        if not area_id and device_id:
            area_id = device_area.get(device_id)
        registry[entity_id] = EntityInfo(entity_id=entity_id, device_id=device_id, area_id=area_id)

    logger.info(f"Fetched registry: {len(registry)} entities")
    return registry
