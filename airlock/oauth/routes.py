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

    async def _complete(request: Request, expected_provider: str | None) -> RedirectResponse:
        code = request.query_params.get("code")
        state_param = request.query_params.get("state")
        error = request.query_params.get("error")

        if error:
            raise HTTPException(400, f"OAuth error: {error}")
        if not code or not state_param:
            raise HTTPException(400, "Missing code or state parameter")

        # The provider is resolved from `state` (set at /authorize), so the single
        # /oauth/callback works for every provider — no per-provider URL needed.
        pending = pending_states.pop(state_param, None)
        if pending is None:
            raise HTTPException(400, "Invalid or expired state parameter")
        if expected_provider is not None and pending.provider_name != expected_provider:
            raise HTTPException(400, "State/provider mismatch")

        provider = providers[pending.provider_name]
        token = await provider.exchange_code(code, code_verifier=pending.code_verifier)
        await k8s_store.write_token(provider.config.refresh_secret.name, target_namespace, token)
        await k8s_store.write_token(
            provider.config.access_secret.name, target_namespace, token, fields=ACCESS_TOKEN_FIELDS
        )
        logger.info(f"Stored tokens for {pending.provider_name} (expires {token.expires_at})")
        return RedirectResponse("/#/oauth")

    @router.get("/callback", response_model=None)
    async def callback(request: Request) -> RedirectResponse:
        """Single shared OAuth callback; the provider is resolved from `state`."""
        return await _complete(request, expected_provider=None)

    # CLEANUP(added 2026-06-29): legacy per-provider callback path, kept so providers
    # still configured with a `/oauth/callback/<name>` redirect_uri (and the matching
    # URI registered in their OAuth app) keep working during the migration to the shared
    # /oauth/callback. Remove once every provider omits `redirect_uri` and the old URIs
    # are deregistered from the OAuth apps.
    @router.get("/callback/{provider_name}", response_model=None)
    async def callback_legacy(provider_name: str, request: Request) -> RedirectResponse:
        if provider_name not in providers:
            raise HTTPException(404, f"Unknown provider: {provider_name}")
        return await _complete(request, expected_provider=provider_name)

    return router
