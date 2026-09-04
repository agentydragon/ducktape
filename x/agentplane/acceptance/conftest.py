"""Fixtures for the live acceptance suite: one client against a deployed app, and sandboxes that
clean up after themselves.

Which instance is under test comes from the environment; the shipped default is the staging
deployment this suite exists for.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from x.agentplane.acceptance.client import SANDBOX_READY_SECONDS, Client, is_running, mint_token
from x.agentplane.app.inventory import NewSandbox, SandboxView

BASE_URL = "AGENTPLANE_ACCEPTANCE_URL"
TOKEN = "AGENTPLANE_ACCEPTANCE_TOKEN"
NAMESPACE = "AGENTPLANE_ACCEPTANCE_NAMESPACE"
SERVICE_ACCOUNT = "AGENTPLANE_ACCEPTANCE_SERVICE_ACCOUNT"

STAGING_URL = "https://agentplane-staging.allegedly.works"
STAGING_NAMESPACE = "agentplane-staging"
STAGING_SERVICE_ACCOUNT = "agentplane-agent"
AUDIENCE = "agentplane"


class SandboxNotReadyError(Exception):
    """The sandbox did not reach Running with a Pod address in time."""


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ.get(BASE_URL, STAGING_URL)


@pytest.fixture(scope="session")
def token() -> str:
    """The caller's own token if it set one, else one minted from the caller's kubeconfig."""
    if (supplied := os.environ.get(TOKEN)) is not None:
        return supplied
    return mint_token(
        namespace=os.environ.get(NAMESPACE, STAGING_NAMESPACE),
        service_account=os.environ.get(SERVICE_ACCOUNT, STAGING_SERVICE_ACCOUNT),
        audience=AUDIENCE,
    )


@pytest.fixture
async def client(base_url: str, token: str) -> AsyncIterator[Client]:
    async with Client(base_url=base_url, token=token) as opened:
        yield opened


async def _running(client: Client, name: str) -> SandboxView:
    """Wait for the sandbox's Pod to be scheduled, pulled and Ready — the condition, not a delay."""
    async for attempt in AsyncRetrying(stop=stop_after_delay(SANDBOX_READY_SECONDS), wait=wait_fixed(2), reraise=True):
        with attempt:
            view = await client.sandbox(name)
            if not is_running(view):
                raise SandboxNotReadyError(f"{name} is {view.state} with pod {view.pod}")
            return view
    raise AssertionError("unreachable: reraise=True either returns the view or raises")


@pytest.fixture
async def sandbox(client: Client) -> AsyncIterator[Callable[..., Awaitable[SandboxView]]]:
    """Creates sandboxes and deletes every one it created, whatever the test did.

    Deletion is refused while a sandbox runs, so each is suspended first. A sandbox that will not go
    fails the teardown rather than being swallowed: a leaked one holds a PVC and a node slot.
    """
    created: list[str] = []

    async def create(slug: str, *, policies: list[str] | None = None) -> SandboxView:
        view = await client.create_sandbox(NewSandbox(slug=slug, policies=policies or []))
        created.append(view.name)
        return await _running(client, view.name)

    yield create

    for name in created:
        await client.suspend_sandbox(name)
        await client.delete_sandbox(name)
