"""Webhook receiver endpoints."""

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatelet.server.auth.webhook_auth import AuthError, create_auth_handler
from gatelet.server.database import get_db_session
from gatelet.server.models import WebhookIntegration, WebhookPayload

router = APIRouter(tags=["webhooks"])

DB_SESSION = Depends(get_db_session)


# Error handler for webhook auth errors should be registered in app.py, not at the router level


@router.post("/webhook/{integration_name}")
async def receive(integration_name: str, request: Request, db_session: AsyncSession = DB_SESSION) -> dict[str, Any]:
    """Receive webhook payload for a specific integration."""
    # Check if integration exists
    query = select(WebhookIntegration).where(WebhookIntegration.name == integration_name)
    result = await db_session.execute(query)
    integration = result.scalar_one_or_none()

    if not integration:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Integration '{integration_name}' not found")

    if not integration.is_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"Integration '{integration_name}' is disabled"
        )

    # Authenticate the request
    try:
        # Get auth handler
        auth_handler = create_auth_handler(integration.auth_config)

        # Extract credentials from request headers
        credentials = None
        if auth_header := request.headers.get("Authorization"):
            scheme, _, credentials_value = auth_header.partition(" ")
            if credentials_value:
                credentials = HTTPAuthorizationCredentials(scheme=scheme, credentials=credentials_value)

        # Validate the credentials
        await auth_handler.validate(request, credentials)
    except AuthError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication failed", headers=e.headers)

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload")

    # Store webhook payload
    new_payload = WebhookPayload(integration_id=integration.id, payload=payload)

    db_session.add(new_payload)
    await db_session.commit()

    return {"status": "ok", "payload_id": new_payload.id}
