from __future__ import annotations

import logging
from typing import Any

import homeassistant_api
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from gatelet.server.auth.dependencies import Auth
from gatelet.server.config import Settings, get_settings
from gatelet.server.shared import make_ha_history_url

logger = logging.getLogger(__name__)

router = APIRouter(tags=["homeassistant"])


async def fetch_states(settings: Settings = Depends(get_settings)) -> list[dict[str, Any]]:
    """Fetch states for configured entities."""
    entities: list[dict[str, Any]] = []
    async with homeassistant_api.Client(
        settings.home_assistant.api_url, settings.home_assistant.api_token, use_async=True, verify_ssl=False
    ) as client:
        for entity_id in settings.home_assistant.entities:
            try:
                state = await client.async_get_state(entity_id=entity_id)
                entities.append(
                    {
                        "entity_id": entity_id,
                        "state": state.state,
                        "last_changed": state.last_changed,
                        "friendly_name": state.attributes.get("friendly_name", entity_id),
                    }
                )
            except Exception as exc:  # pragma: no cover - network errors
                logger.error("Failed fetching %s: %s", entity_id, exc)
    return entities


@router.get("/ha/", response_class=HTMLResponse)
async def list_entities(request: Request, auth: Auth, settings: Settings = Depends(get_settings)) -> HTMLResponse:
    """List configured Home Assistant entity states."""
    states = await fetch_states(settings)
    is_human = auth.auth_type == "admin"
    return request.app.state.templates.TemplateResponse(
        "ha_entities.html",
        {
            "request": request,
            "auth": auth,
            "states": states,
            "header": "Entities",
            "is_human": is_human,
            "history": [],
            "ha_history_url": make_ha_history_url(settings),
        },
    )


@router.get("/ha/{entity_id}", response_class=HTMLResponse)
async def entity_details(
    request: Request, entity_id: str, auth: Auth, settings: Settings = Depends(get_settings)
) -> HTMLResponse:
    """Display details for a single entity."""
    states = await fetch_states(settings)
    entity = next((s for s in states if s["entity_id"] == entity_id), None)
    is_human = auth.auth_type == "admin"
    return request.app.state.templates.TemplateResponse(
        "ha_entity.html",
        {
            "request": request,
            "auth": auth,
            "state": entity,
            "header": f"{entity_id} Details",
            "is_human": is_human,
            "history": [],
            "ha_history_url": make_ha_history_url(settings),
        },
    )
