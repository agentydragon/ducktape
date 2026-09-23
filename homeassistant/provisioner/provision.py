"""Install configured Home Assistant components and complete configured onboarding."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import typer

from homeassistant.provisioner.client import HomeAssistantClient, OnboardingStep
from homeassistant.provisioner.component_installer import install_components
from homeassistant.provisioner.endpoint import HomeAssistantEndpoint
from homeassistant.provisioner.settings import HttpConfig, ProvisionerSettings, load_settings

app = typer.Typer(add_completion=False)


def _config_without_metadata(config: object) -> dict[str, object]:
    """Validate and remove runtime metadata from a stored HTTP config."""
    if not isinstance(config, dict):
        raise TypeError(f"Home Assistant returned an invalid HTTP config: {config!r}")
    return {key: value for key, value in config.items() if key not in {"created_at", "error", "error_message"}}


async def configure_http(client: HomeAssistantClient, http_config: HttpConfig, username: str, password: str) -> None:
    """Converge Home Assistant's UI-managed HTTP settings through its admin API."""
    wanted = http_config.model_dump()
    current = await client.websocket_command({"id": 1, "type": "http/config"})
    if not isinstance(current, dict):
        raise TypeError(f"Home Assistant returned an invalid HTTP config response: {current!r}")
    stable = _config_without_metadata(current.get("stable"))
    pending = current.get("pending")
    pending_config = _config_without_metadata(pending) if pending is not None else None
    if stable == wanted and pending is None:
        return
    if pending_config == wanted and current.get("active_config_type") == "pending":
        await client.websocket_command({"id": 1, "type": "http/config/promote"})
        return

    result = await client.websocket_command({"id": 1, "type": "http/config/configure", "config": wanted})
    if not isinstance(result, dict) or not isinstance(result.get("restart"), bool):
        raise TypeError(f"Home Assistant returned an invalid HTTP configure response: {result!r}")
    if not result["restart"]:
        return

    await client.wait_until_ready()
    await client.login(username, password)
    await client.websocket_command({"id": 1, "type": "http/config/promote"})


async def provision(client: HomeAssistantClient, settings: ProvisionerSettings, password: str) -> None:
    """Create the owner if necessary and finish all onboarding steps."""
    completed = await client.wait_until_ready()
    required_steps = frozenset(OnboardingStep)
    if completed is None or completed >= required_steps:
        await client.login(settings.username, password)
        await configure_http(client, settings.http_config, settings.username, password)
        print("Home Assistant onboarding is already complete")
        return

    if OnboardingStep.USER in completed:
        await client.login(settings.username, password)
    else:
        await client.create_owner(settings.display_name, settings.username, password)
    if OnboardingStep.CORE_CONFIG not in completed:
        await client.request_json("/api/onboarding/core_config", data={})
    if OnboardingStep.INTEGRATION not in completed:
        await client.request_json(
            "/api/onboarding/integration", data={"client_id": settings.client_id, "redirect_uri": settings.redirect_uri}
        )
    if OnboardingStep.ANALYTICS not in completed:
        await client.request_json("/api/onboarding/analytics", data={})
    await configure_http(client, settings.http_config, settings.username, password)
    print("Home Assistant onboarding is complete")


async def _setup(settings: ProvisionerSettings) -> None:
    async with httpx2.AsyncClient() as http_client:
        await install_components(http_client, Path("/config"), settings.components)
        if settings.onboarding_enabled:
            if settings.local_admin_password is None:
                raise ValueError("HOME_ASSISTANT_PROVISIONER_LOCAL_ADMIN_PASSWORD is required")
            endpoint = HomeAssistantEndpoint(
                url=settings.home_assistant_url, client_id=settings.client_id, redirect_uri=settings.redirect_uri
            )
            await provision(
                HomeAssistantClient(http_client, endpoint), settings, settings.local_admin_password.get_secret_value()
            )


# A callback keeps `setup` a named subcommand, which the Deployment's init container and the
# onboarding Job invoke; Typer runs a lone command without its name otherwise.
@app.callback()
def main() -> None:
    """Provision Home Assistant."""


@app.command()
def setup() -> None:
    """Install configured components and complete configured onboarding."""
    asyncio.run(_setup(load_settings()))


if __name__ == "__main__":
    app()
