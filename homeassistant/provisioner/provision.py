"""Orchestrate configured Home Assistant provisioning: `setup` installs components and onboards,
`tokens` keeps other workloads' token Secrets valid."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import kubernetes
import typer
from client import HomeAssistantClient, OnboardingStep
from component_installer import install_components
from settings import ProvisionerSettings, load_settings
from tokens import provision_token

app = typer.Typer(add_completion=False)


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


def _local_admin_password(settings: ProvisionerSettings) -> str:
    if settings.local_admin_password is None:
        raise ValueError("HOME_ASSISTANT_PROVISIONER_LOCAL_ADMIN_PASSWORD is required")
    return settings.local_admin_password.get_secret_value()


async def _setup(settings: ProvisionerSettings) -> None:
    async with httpx2.AsyncClient() as http_client:
        await install_components(http_client, Path("/config"), settings.components)
        if settings.onboarding_enabled:
            await provision(HomeAssistantClient(http_client, settings), _local_admin_password(settings))


async def _keep_tokens(settings: ProvisionerSettings) -> None:
    if settings.tokens is None:
        raise ValueError("HOME_ASSISTANT_PROVISIONER_TOKENS is required")
    kubernetes.config.load_incluster_config()
    v1 = kubernetes.client.CoreV1Api()
    async with httpx2.AsyncClient() as http_client:
        client = HomeAssistantClient(http_client, settings)
        for token in settings.tokens:
            await provision_token(client, v1, token, _local_admin_password(settings))


@app.command()
def setup() -> None:
    """Install configured components and complete configured onboarding."""
    asyncio.run(_setup(load_settings()))


@app.command()
def tokens() -> None:
    """Keep each configured Secret holding a long-lived token Home Assistant accepts."""
    asyncio.run(_keep_tokens(load_settings()))


if __name__ == "__main__":
    app()
