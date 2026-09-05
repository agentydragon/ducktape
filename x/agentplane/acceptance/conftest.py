"""Fixtures for the live acceptance suite: a client against a deployed app, both harnesses, and
sandboxes that clean up after themselves.

Which instance is under test comes from the environment; the shipped default is the staging
deployment this suite exists for.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from x.agentplane.app.api import Provider
from x.agentplane.app.client import Client, is_running
from x.agentplane.app.inventory import NewSandbox, SandboxView
from x.agentplane.app.presets import ThreadDefaults

BASE_URL = "AGENTPLANE_ACCEPTANCE_URL"
TOKEN = "AGENTPLANE_ACCEPTANCE_TOKEN"
NAMESPACE = "AGENTPLANE_ACCEPTANCE_NAMESPACE"
SERVICE_ACCOUNT = "AGENTPLANE_ACCEPTANCE_SERVICE_ACCOUNT"

STAGING_URL = "https://agentplane-staging.allegedly.works"
STAGING_NAMESPACE = "agentplane-staging"
STAGING_SERVICE_ACCOUNT = "agentplane-agent"
AUDIENCE = "agentplane"

# A sandbox's first Pod has to be scheduled and pull the runner image.
SANDBOX_READY_SECONDS = 300.0


class TokenNotMintedError(Exception):
    """`kubectl create token` refused; its own message says why."""


class SandboxNotReadyError(Exception):
    """The sandbox did not reach Running with a Pod address in time."""


def mint_token(*, namespace: str, service_account: str, audience: str, lifetime: str = "1800s") -> str:
    """An audience-scoped token for `service_account`, from whatever kubeconfig the caller holds.

    RBAC on `serviceaccounts/token` is what gates this. The audience is chosen freely by whoever
    asks and so proves nothing on its own, which is why the app checks the subject as well.

    Bazel scrubs the environment, so the target inherits HOME and KUBECONFIG: without either,
    kubectl finds no kubeconfig and quietly falls back to `http://localhost:8080`.
    """
    command = [
        "kubectl",
        "-n",
        namespace,
        "create",
        "token",
        service_account,
        f"--audience={audience}",
        f"--duration={lifetime}",
    ]
    minted = subprocess.run(command, capture_output=True, text=True, check=False)
    if minted.returncode != 0:
        # kubectl says what is wrong -- no kubeconfig, no RBAC, no such ServiceAccount -- and
        # CalledProcessError would hide all of it behind an exit status.
        raise TokenNotMintedError(f"`{' '.join(command)}` failed with {minted.returncode}: {minted.stderr.strip()}")
    return minted.stdout.strip()


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


@pytest.fixture(params=list(Provider), ids=[str(provider) for provider in Provider])
def provider(request: pytest.FixtureRequest) -> Provider:
    """Every scenario runs on every harness: one runner protocol, so one test body covers both."""
    assert isinstance(request.param, Provider)
    return request.param


@pytest.fixture
async def model(client: Client, provider: Provider) -> str:
    """A model this deployment offers for this harness, asked of the app rather than hardcoded."""
    catalog = await client.models()
    offered = catalog[provider]
    assert offered, f"the deployment offers no model for {provider}"
    return offered[0]


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
    fails the teardown rather than being swallowed: a leaked one holds a PVC and a node slot. A run
    killed outright still leaks — see the sweeper entry in this package's README.
    """
    created: list[str] = []

    async def create(
        slug: str,
        *,
        policies: list[str] | None = None,
        preset: str | None = None,
        thread_defaults: ThreadDefaults | None = None,
    ) -> SandboxView:
        values: dict[str, object] = {"slug": slug, "preset": preset, "thread_defaults": thread_defaults}
        if policies is not None:
            values["policies"] = policies
        view = await client.create_sandbox(NewSandbox.model_validate(values))
        created.append(view.name)
        return await _running(client, view.name)

    yield create

    for name in created:
        await client.suspend_sandbox(name)
        await client.delete_sandbox(name)
