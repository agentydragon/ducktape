"""Operator-facing proxy for the aiquota service."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import SecretStr

from aiquota.models import AllQuotas
from aiquota.render.view_model import AllQuotasView, to_view


def build_router(*, url: str | None, bearer_token: SecretStr | None) -> APIRouter:
    router = APIRouter(prefix="/api/aiquota")

    @router.get("/quotas", response_model=AllQuotasView)
    async def quotas() -> AllQuotasView:
        if not url or not bearer_token:
            raise HTTPException(status_code=503, detail="aiquota is not configured")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"{url.rstrip('/')}/v1/quotas",
                    headers={"Authorization": f"Bearer {bearer_token.get_secret_value()}"},
                )
            response.raise_for_status()
            return to_view(AllQuotas.model_validate_json(response.content))
        except httpx.HTTPStatusError as error:
            raise HTTPException(
                status_code=502, detail=f"aiquota returned HTTP {error.response.status_code}"
            ) from error
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(status_code=502, detail="aiquota is unavailable") from error

    return router
