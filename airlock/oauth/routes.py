"""FastAPI routes for OAuth authorization/callback flows.

These routes handle browser-based OAuth redirects that cannot go through MCP.
"""

import logging
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from airlock.oauth.k8s_client import K8sTokenStore
from airlock.oauth.provider import ACCESS_TOKEN_FIELDS, GenericOAuth2Provider, generate_pkce_pair

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PendingState:
    provider_name: str
    code_verifier: str | None


def create_oauth_router(
    providers: dict[str, GenericOAuth2Provider], k8s_store: K8sTokenStore, target_namespace: str
) -> APIRouter:
    """Create a FastAPI router for OAuth authorization/callback flows."""
    router = APIRouter(prefix="/oauth", tags=["oauth"])
    # state -> pending state. Per-instance, in-memory, not persisted: a pod
    # restart between /authorize and /callback aborts the user's auth flow,
    # which is acceptable for human-initiated OAuth (seconds-long lifetime).
    pending_states: dict[str, _PendingState] = {}

    @router.get("/authorize/{provider_name}", response_model=None)
    async def authorize(provider_name: str) -> RedirectResponse:
        provider = providers.get(provider_name)
        if provider is None:
            raise HTTPException(404, f"Unknown provider: {provider_name}")
        state = provider.generate_state()
        code_verifier: str | None = None
        code_challenge: str | None = None
        if provider.config.use_pkce:
            code_verifier, code_challenge = generate_pkce_pair()
        pending_states[state] = _PendingState(provider_name=provider_name, code_verifier=code_verifier)
        url = provider.build_authorize_url(state, code_challenge=code_challenge)
        return RedirectResponse(url)

    @router.get("/callback/{provider_name}", response_model=None)
    async def callback_get(provider_name: str, request: Request) -> RedirectResponse:
        provider = providers.get(provider_name)
        if provider is None:
            raise HTTPException(404, f"Unknown provider: {provider_name}")

        code = request.query_params.get("code")
        state_param = request.query_params.get("state")
        error = request.query_params.get("error")

        if error:
            raise HTTPException(400, f"OAuth error: {error}")
        if not code or not state_param:
            raise HTTPException(400, "Missing code or state parameter")

        pending = pending_states.pop(state_param, None)
        if pending is None:
            raise HTTPException(400, "Invalid or expired state parameter")
        if pending.provider_name != provider_name:
            raise HTTPException(400, "State/provider mismatch")

        token = await provider.exchange_code(code, code_verifier=pending.code_verifier)
        await k8s_store.write_token(provider.config.refresh_secret.name, target_namespace, token)
        await k8s_store.write_token(
            provider.config.access_secret.name, target_namespace, token, fields=ACCESS_TOKEN_FIELDS
        )
        logger.info(f"Stored tokens for {provider_name} (expires {token.expires_at})")
        return RedirectResponse("/#/oauth")

    return router
