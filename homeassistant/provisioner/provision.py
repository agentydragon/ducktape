"""Orchestrate configured Home Assistant provisioning."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import kubernetes
from client import HomeAssistantClient, OnboardingStep
from component_installer import install_components
from settings import load_settings
from tokens import provision_token


async def provision(client: HomeAssistantClient, password: str) -> None:
    """Create the owner if necessary and finish all onboarding steps."""
    settings = client.settings
    completed = await client.wait_until_ready()
    required_steps = frozenset(OnboardingStep)
    if completed is None or completed >= required_steps:
        await client.login(settings.username, password)
        await client.configure_http(password)
        print("Home Assistant onboarding is already complete")
        return

    if OnboardingStep.USER in completed:
        await client.login(settings.username, password)
    else:
        await client.create_owner(password)
    if OnboardingStep.CORE_CONFIG not in completed:
        await client.request_json("/api/onboarding/core_config", data={})
    if OnboardingStep.INTEGRATION not in completed:
        await client.request_json(
            "/api/onboarding/integration", data={"client_id": settings.client_id, "redirect_uri": settings.redirect_uri}
        )
    if OnboardingStep.ANALYTICS not in completed:
        await client.request_json("/api/onboarding/analytics", data={})
    await client.configure_http(password)
    print("Home Assistant onboarding is complete")


async def main() -> None:
    """Install configured components, complete configured onboarding, then keep the configured
    token Secrets valid."""
    settings = load_settings()
    config_dir = Path("/config")
    async with httpx2.AsyncClient() as http_client:
        await install_components(http_client, config_dir, settings.components)
        if not settings.onboarding_enabled and settings.tokens is None:
            return
        if settings.local_admin_password is None:
            raise ValueError("HOME_ASSISTANT_PROVISIONER_LOCAL_ADMIN_PASSWORD is required for onboarding and tokens")
        password = settings.local_admin_password.get_secret_value()
        client = HomeAssistantClient(http_client, settings)
        if settings.onboarding_enabled:
            await provision(client, password)
        if settings.tokens is not None:
            kubernetes.config.load_incluster_config()
            v1 = kubernetes.client.CoreV1Api()
            for token in settings.tokens:
                await provision_token(client, v1, token, password)


if __name__ == "__main__":
    asyncio.run(main())
