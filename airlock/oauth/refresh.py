"""Background token refresh loop."""

import asyncio
import logging
from collections.abc import Mapping

from airlock.oauth.k8s_client import K8sTokenStore
from airlock.oauth.provider import ACCESS_TOKEN_FIELDS, Provider

logger = logging.getLogger(__name__)


async def token_refresh_loop(
    providers: Mapping[str, Provider], k8s_store: K8sTokenStore, target_namespace: str, check_interval: float = 300
) -> None:
    """Check all provider tokens periodically, refresh if near expiry, and clean up orphaned secrets."""
    known_secret_names = frozenset(
        name
        for provider in providers.values()
        for name in (provider.config.refresh_secret.name, provider.config.access_secret.name)
    )
    while True:
        for name, provider in providers.items():
            try:
                token = await k8s_store.read_token(provider.config.refresh_secret.name, target_namespace)
                if token is None:
                    continue
                if not provider.needs_refresh(token):
                    continue
                logger.info("Refreshing token for %s (expires %s)", name, token.expires_at)
                new_token = await provider.refresh_tokens(token.refresh_token)
                await k8s_store.write_token(
                    provider.config.refresh_secret.name,
                    target_namespace,
                    new_token,
                    annotations=provider.config.refresh_secret.annotations or None,
                )
                await k8s_store.write_token(
                    provider.config.access_secret.name,
                    target_namespace,
                    new_token,
                    annotations=provider.config.access_secret.annotations or None,
                    fields=ACCESS_TOKEN_FIELDS,
                )
                logger.info("Refreshed token for %s (new expiry %s)", name, new_token.expires_at)
            except Exception:
                logger.exception("Failed to refresh token for %s", name)
        try:
            await k8s_store.delete_orphaned_secrets(target_namespace, known_secret_names)
        except Exception:
            logger.exception("Failed to clean up orphaned secrets")
        await asyncio.sleep(check_interval)
