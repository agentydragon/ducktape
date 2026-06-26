"""Background token refresh loop."""

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime

from airlock.oauth.k8s_client import K8sTokenStore
from airlock.oauth.provider import ACCESS_TOKEN_FIELDS, GenericOAuth2Provider

logger = logging.getLogger(__name__)


def check_scope_drift(
    provider_name: str, requested_scopes: list[str], granted_scope: str, warned: set[tuple[str, str]]
) -> None:
    """Warn once per (provider, granted-scope) when configured scopes differ from the token's grant.

    Refresh tokens can't change scopes — the user must re-authorize via /oauth/authorize/{provider}.
    The `warned` set is mutated to de-dupe noise across refresh-loop iterations.
    """
    requested = set(requested_scopes)
    granted = set(granted_scope.split())
    if requested == granted:
        return
    key = (provider_name, granted_scope)
    if key in warned:
        return
    warned.add(key)
    logger.warning(
        f"Scope drift for {provider_name}: requested={sorted(requested)} granted={sorted(granted)} "
        f"missing={sorted(requested - granted)} extra={sorted(granted - requested)}. "
        f"Re-authorize at /oauth/authorize/{provider_name} to pick up new scopes."
    )


async def token_refresh_loop(
    providers: Mapping[str, GenericOAuth2Provider],
    k8s_store: K8sTokenStore,
    target_namespace: str,
    check_interval: float = 300,
    refresh_errors: dict[str, str] | None = None,
) -> None:
    """Check all provider tokens periodically, refresh if near expiry, and clean up orphaned secrets.

    refresh_errors: if provided, mutated in-place: cleared on success, set to exception repr on failure.
    """
    known_secret_names = frozenset(
        name
        for provider in providers.values()
        for name in (provider.config.refresh_secret.name, provider.config.access_secret.name)
    )
    warned_scope_drifts: set[tuple[str, str]] = set()
    while True:
        for name, provider in providers.items():
            token = None
            try:
                token = await k8s_store.read_token(provider.config.refresh_secret.name, target_namespace)
                if token is None:
                    continue
                check_scope_drift(name, provider.config.scopes, token.scope, warned_scope_drifts)
                if not provider.needs_refresh(token):
                    continue
                logger.info(f"Refreshing token for {name} (expires {token.expires_at})")
                new_token = await provider.refresh_tokens(token.refresh_token)
                await k8s_store.write_token(provider.config.refresh_secret.name, target_namespace, new_token)
                await k8s_store.write_token(
                    provider.config.access_secret.name, target_namespace, new_token, fields=ACCESS_TOKEN_FIELDS
                )
                logger.info(f"Refreshed token for {name} (new expiry {new_token.expires_at})")
                if refresh_errors is not None:
                    refresh_errors.pop(name, None)
            except Exception as exc:
                logger.exception(f"Failed to refresh token for {name}")
                if refresh_errors is not None:
                    refresh_errors[name] = repr(exc)
                # Delete the access secret if the token is actually expired so
                # downstream consumers don't get a stale nonfunctional bearer token.
                if token is None:
                    logger.warning(
                        f"Leaving access secret for {name} unchanged after refresh failure because "
                        "the refresh token could not be read."
                    )
                elif datetime.now(UTC) >= token.expires_at:
                    logger.warning(
                        f"Deleting access secret for {name} because refresh failed and "
                        f"the token expired at {token.expires_at}."
                    )
                    try:
                        await k8s_store.delete_secret(provider.config.access_secret.name, target_namespace)
                    except Exception:
                        logger.exception(f"Failed to delete expired access secret for {name}")
                else:
                    logger.warning(
                        f"Leaving access secret for {name} unchanged after refresh failure because "
                        f"the token remains valid until {token.expires_at}."
                    )
        try:
            await k8s_store.delete_orphaned_secrets(target_namespace, known_secret_names)
        except Exception:
            logger.exception("Failed to clean up orphaned secrets")
        await asyncio.sleep(check_interval)
