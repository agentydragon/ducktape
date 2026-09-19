"""Orchestrate configured Home Assistant provisioning."""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiohttp
from client import HomeAssistantClient, OnboardingStep
from component_installer import install_components
from settings import load_settings


async def provision(client: HomeAssistantClient, password: str) -> None:
    """Create the owner if necessary and finish all onboarding steps."""
    settings = client.settings
    completed = await client.wait_until_ready()
    required_steps = frozenset(OnboardingStep)
    if completed is None or completed >= required_steps:
        await client.login(password)
        await client.configure_http(password)
        print("Home Assistant onboarding is already complete")
        return

    if OnboardingStep.USER in completed:
        await client.login(password)
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
    """Install configured components and complete configured onboarding."""
    settings = load_settings()
    config_dir = Path("/config")
    async with aiohttp.ClientSession() as session:
        await install_components(session, config_dir, settings.components)
        if settings.onboarding_enabled:
            if settings.local_admin_password is None:
                raise ValueError("HOME_ASSISTANT_PROVISIONER_LOCAL_ADMIN_PASSWORD is required for onboarding")
            client = HomeAssistantClient(session, settings)
            await provision(client, settings.local_admin_password.get_secret_value())


if __name__ == "__main__":
    asyncio.run(main())
