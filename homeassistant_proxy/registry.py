"""Fetch entity/device/area registries from Home Assistant via WebSocket API."""

import json
import logging
from typing import Any

import websockets.asyncio.client

from homeassistant_proxy.policy import EntityInfo

logger = logging.getLogger(__name__)


async def _ws_command(ws: websockets.asyncio.client.ClientConnection, msg_id: int, command: dict[str, Any]) -> Any:
    """Send a WebSocket command and return the result."""
    command["id"] = msg_id
    await ws.send(json.dumps(command))
    response = json.loads(await ws.recv())
    if not response.get("success", False):
        raise RuntimeError(f"HA WebSocket command {command.get('type')} failed: {response}")
    return response["result"]


async def fetch_registry(ha_url: str, ha_token: str) -> dict[str, EntityInfo]:
    """Connect to HA WebSocket, fetch registries, return entity_id -> EntityInfo mapping.

    Connects, authenticates, queries entity and device registries, then disconnects.
    """
    ws_url = ha_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url}/api/websocket"

    async with websockets.asyncio.client.connect(ws_url) as ws:
        # HA sends auth_required on connect
        auth_required = json.loads(await ws.recv())
        if auth_required.get("type") != "auth_required":
            raise RuntimeError(f"Expected auth_required, got: {auth_required}")

        # Authenticate
        await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
        auth_result = json.loads(await ws.recv())
        if auth_result.get("type") != "auth_ok":
            raise RuntimeError(f"HA auth failed: {auth_result}")

        # Fetch entity registry (entity_id -> device_id, area_id)
        entities_raw = await _ws_command(ws, 1, {"type": "config/entity_registry/list"})

        # Fetch device registry (device_id -> area_id, for fallback)
        devices_raw = await _ws_command(ws, 2, {"type": "config/device_registry/list"})

    # Build device_id -> area_id mapping from device registry
    device_area: dict[str, str | None] = {}
    for device in devices_raw:
        device_id = device.get("id")
        if device_id:
            device_area[device_id] = device.get("area_id")

    # Build entity_id -> EntityInfo mapping
    registry: dict[str, EntityInfo] = {}
    for entity in entities_raw:
        entity_id = entity.get("entity_id")
        if not entity_id:
            continue
        device_id = entity.get("device_id")
        # Entity's own area_id takes precedence; fall back to device's area_id
        area_id = entity.get("area_id")
        if not area_id and device_id:
            area_id = device_area.get(device_id)
        registry[entity_id] = EntityInfo(entity_id=entity_id, device_id=device_id, area_id=area_id)

    logger.info(f"Fetched registry: {len(registry)} entities")
    return registry
