"""Complete Home Assistant's onboarding and converge its UI-managed HTTP settings."""

from __future__ import annotations

import asyncio

import httpx2

from homeassistant.provisioner.client import HomeAssistantClient, OnboardingStep
from homeassistant.provisioner.onboarding.settings import HttpConfig, Settings


def _config_without_metadata(config: object) -> dict[str, object]:
    """Validate and remove runtime metadata from a stored HTTP config."""
    if not isinstance(config, dict):
        raise TypeError(f"Home Assistant returned an invalid HTTP config: {config!r}")
    return {key: value for key, value in config.items() if key not in {"created_at", "error", "error_message"}}


async def configure_http(client: HomeAssistantClient, http_config: HttpConfig, username: str, password: str) -> None:
    """Converge Home Assistant's UI-managed HTTP settings through its admin API."""
    wanted = http_config.model_dump()
    current = await client.websocket_command({"type": "http/config"})
    if not isinstance(current, dict):
        raise TypeError(f"Home Assistant returned an invalid HTTP config response: {current!r}")
    stable = _config_without_metadata(current.get("stable"))
    pending = current.get("pending")
    pending_config = _config_without_metadata(pending) if pending is not None else None
    if stable == wanted and pending is None:
        return
    if pending_config == wanted and current.get("active_config_type") == "pending":
        await client.websocket_command({"type": "http/config/promote"})
        return

    result = await client.websocket_command({"type": "http/config/configure", "config": wanted})
    if not isinstance(result, dict) or not isinstance(result.get("restart"), bool):
        raise TypeError(f"Home Assistant returned an invalid HTTP configure response: {result!r}")
    if not result["restart"]:
        return

    await client.wait_until_ready()
    await client.login(username, password)
    await client.websocket_command({"type": "http/config/promote"})


async def onboard(client: HomeAssistantClient, settings: Settings) -> None:
    """Create the owner if necessary and finish all onboarding steps."""
    password = settings.owner_password.get_secret_value()
    completed = await client.wait_until_ready()
    required_steps = frozenset(OnboardingStep)
    if completed is None or completed >= required_steps:
        await client.login(settings.owner_username, password)
        await configure_http(client, settings.http_config, settings.owner_username, password)
        print("Home Assistant onboarding is already complete")
        return

    if OnboardingStep.USER in completed:
        await client.login(settings.owner_username, password)
    else:
        await client.create_owner(settings.owner_display_name, settings.owner_username, password)
    if OnboardingStep.CORE_CONFIG not in completed:
        await client.request_json("/api/onboarding/core_config", data={})
    if OnboardingStep.INTEGRATION not in completed:
        await client.request_json(
            "/api/onboarding/integration",
            data={"client_id": settings.endpoint.client_id, "redirect_uri": settings.endpoint.redirect_uri},
        )
    if OnboardingStep.ANALYTICS not in completed:
        await client.request_json("/api/onboarding/analytics", data={})
    await configure_http(client, settings.http_config, settings.owner_username, password)
    print("Home Assistant onboarding is complete")


async def async_main(settings: Settings) -> None:
    async with httpx2.AsyncClient() as http_client:
        await onboard(HomeAssistantClient(http_client, settings.endpoint), settings)


def main() -> None:
    asyncio.run(async_main(Settings()))


if __name__ == "__main__":
    main()
