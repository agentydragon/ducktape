"""FastAPI routes for OAuth authorization/callback flows.

These routes handle browser-based redirects that cannot go through MCP or REST:
- GET  /oauth/authorize/{provider_name}  — initiate OAuth redirect or return Plaid link_token
- GET  /oauth/callback/{provider_name}   — OAuth2 callback / Plaid OAuth resume
- POST /oauth/callback/{provider_name}   — Plaid public_token exchange
"""

import logging
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from airlock.oauth.k8s_client import K8sTokenStore
from airlock.oauth.provider import (
    ACCESS_TOKEN_FIELDS,
    GenericOAuth2Provider,
    PlaidProvider,
    Provider,
    generate_pkce_pair,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PendingState:
    provider_name: str
    code_verifier: str | None


class _PlaidCallbackBody(BaseModel):
    public_token: str


class _PlaidLinkResponse(BaseModel):
    link_token: str
    received_redirect_uri: str | None = None


def create_oauth_router(providers: dict[str, Provider], k8s_store: K8sTokenStore, target_namespace: str) -> APIRouter:
    """Create a FastAPI router for OAuth authorization/callback flows."""
    router = APIRouter(prefix="/oauth", tags=["oauth"])
    # state -> pending state. Per-instance, in-memory, not persisted: a pod
    # restart between /authorize and /callback aborts the user's auth flow,
    # which is acceptable for human-initiated OAuth (seconds-long lifetime).
    pending_states: dict[str, _PendingState] = {}

    @router.get("/authorize/{provider_name}", response_model=None)
    async def authorize(provider_name: str) -> RedirectResponse | _PlaidLinkResponse:
        provider = providers.get(provider_name)
        if provider is None:
            raise HTTPException(404, f"Unknown provider: {provider_name}")
        state = provider.generate_state()
        if isinstance(provider, PlaidProvider):
            pending_states[state] = _PendingState(provider_name=provider_name, code_verifier=None)
            link_token = await provider.create_link_token(state)
            return _PlaidLinkResponse(link_token=link_token)
        code_verifier: str | None = None
        code_challenge: str | None = None
        if isinstance(provider, GenericOAuth2Provider) and provider.config.use_pkce:
            code_verifier, code_challenge = generate_pkce_pair()
        pending_states[state] = _PendingState(provider_name=provider_name, code_verifier=code_verifier)
        url = provider.build_authorize_url(state, code_challenge=code_challenge)
        return RedirectResponse(url)

    @router.get("/callback/{provider_name}", response_model=None)
    async def callback_get(provider_name: str, request: Request) -> RedirectResponse | _PlaidLinkResponse:
        provider = providers.get(provider_name)
        if provider is None:
            raise HTTPException(404, f"Unknown provider: {provider_name}")

        # Plaid OAuth institution mid-flow: bank redirected here with oauth_state_id.
        # Return a link_token with receivedRedirectUri so the frontend can resume.
        if isinstance(provider, PlaidProvider):
            oauth_state_id = request.query_params.get("oauth_state_id")
            if oauth_state_id is None:
                raise HTTPException(400, "Plaid callback missing oauth_state_id")
            state = provider.generate_state()
            pending_states[state] = _PendingState(provider_name=provider_name, code_verifier=None)
            link_token = await provider.create_link_token(state)
            return _PlaidLinkResponse(link_token=link_token, received_redirect_uri=str(request.url))

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

    @router.post("/callback/{provider_name}")
    async def callback_post(provider_name: str, body: _PlaidCallbackBody) -> RedirectResponse:
        """Receive the public_token from the Plaid Link JS widget and exchange it."""
        provider = providers.get(provider_name)
        if provider is None:
            raise HTTPException(404, f"Unknown provider: {provider_name}")
        if not isinstance(provider, PlaidProvider):
            raise HTTPException(405, f"{provider_name} does not support POST callback")
        token = await provider.exchange_public_token(body.public_token)
        await k8s_store.write_token(provider.config.refresh_secret.name, target_namespace, token)
        await k8s_store.write_token(
            provider.config.access_secret.name, target_namespace, token, fields=ACCESS_TOKEN_FIELDS
        )
        logger.info(f"Stored Plaid tokens for {provider_name}")
        return RedirectResponse("/#/oauth", status_code=303)

    return router
