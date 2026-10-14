"""Fixtures for the live acceptance suite: a client against a deployed app, both harnesses, and
sandboxes that clean up after themselves.

Which instance is under test comes from the environment; the shipped default is the isolated
testing deployment. Staging remains available for explicit ad hoc runs.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from typing import cast
from uuid import UUID

import pytest
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.acceptance.ollama_warmup import warm_ollama
from agentplane.app.sandbox_models import NewSandbox, SandboxView, SessionDefaults, sandbox_is_ready
from agentplane.app.testing.client import Client
from agentplane.proto import runner_pb2
from agentplane.runner.harness import Harness
from agentplane.sandbox_resource import OperatingMode

# `runner_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf

BASE_URL = "AGENTPLANE_ACCEPTANCE_URL"
TOKEN = "AGENTPLANE_ACCEPTANCE_TOKEN"
NAMESPACE = "AGENTPLANE_ACCEPTANCE_NAMESPACE"
SERVICE_ACCOUNT = "AGENTPLANE_ACCEPTANCE_SERVICE_ACCOUNT"

TESTING_URL = "https://agentplane-testing.allegedly.works"
TESTING_NAMESPACE = "agentplane-testing"
TESTING_SERVICE_ACCOUNT = "agentplane-agent"
AUDIENCE = "agentplane"

# The Pod SandboxTemplate every deployment offers; testing also offers a VM one, so the default is named.
POD_TEMPLATE = "runner"

# A sandbox's first Pod has to be scheduled and pull the runner image.
SANDBOX_READY_SECONDS = 300.0

# What the `sandbox` fixture yields: create a sandbox by name and wait until it runs.
Sandboxes = Callable[..., Awaitable[SandboxView]]


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
    return os.environ.get(BASE_URL, TESTING_URL)


@pytest.fixture(scope="session")
def token() -> str:
    """The caller's own token if it set one, else one minted from the caller's kubeconfig."""
    if (supplied := os.environ.get(TOKEN)) is not None:
        return supplied
    return mint_token(
        namespace=os.environ.get(NAMESPACE, TESTING_NAMESPACE),
        service_account=os.environ.get(SERVICE_ACCOUNT, TESTING_SERVICE_ACCOUNT),
        audience=AUDIENCE,
    )


@pytest.fixture
async def client(base_url: str, token: str) -> AsyncIterator[Client]:
    async with Client(base_url=base_url, token=token) as opened:
        yield opened


@pytest.fixture(params=[runner_pb2.HARNESS_CLAUDE, runner_pb2.HARNESS_CODEX], ids=["claude", "codex"])
def harness(request: pytest.FixtureRequest) -> runner_pb2.Harness:
    """Every scenario runs on every harness: one runner protocol, so one test body covers both."""
    return cast(runner_pb2.Harness, request.param)


@pytest.fixture
async def model(client: Client, harness: runner_pb2.Harness) -> str:
    """A model this deployment offers for this harness, asked of the app rather than hardcoded."""
    catalog = await client.models()
    config_harness = Harness(runner_pb2.Harness.Name(harness))
    offered = catalog.harnesses[config_harness]
    assert offered, f"the deployment offers no model for {harness}"
    await warm_ollama(offered[0])
    return offered[0]


async def wait_until_running(client: Client, name: str) -> SandboxView:
    """Wait for the Sandbox's own `Ready` condition — the condition, not a delay."""
    async for attempt in AsyncRetrying(stop=stop_after_delay(SANDBOX_READY_SECONDS), wait=wait_fixed(2), reraise=True):
        with attempt:
            view = await client.sandbox(name)
            if not sandbox_is_ready(view):
                raise SandboxNotReadyError(f"{name} is not Ready: {view.status}")
            return view
    raise AssertionError("unreachable: reraise=True either returns the view or raises")


async def suspend_and_delete(client: Client, name: str, uid: UUID) -> None:
    await client.suspend_sandbox(name)
    # Deletion admission reads effective mode, which the controller publishes after the spec write.
    async with asyncio.timeout(SANDBOX_READY_SECONDS), aclosing(client.sandbox_snapshots()) as snapshots:
        async for snapshot in snapshots:
            if not snapshot.watch.fresh:
                continue
            view = next((view for view in snapshot.sandboxes if view.name == name), None)
            if view is None:
                continue
            if view.uid != uid:
                raise SandboxNotReadyError(f"{name} has been replaced: expected {uid}, observed {view.uid}")
            if view.operating_mode is OperatingMode.SUSPENDED:
                break
        else:
            raise SandboxNotReadyError(f"{name}'s stream ended before observing suspension")
    await client.delete_sandbox(name)


@pytest.fixture
async def sandbox(client: Client) -> AsyncIterator[Sandboxes]:
    """Creates sandboxes and deletes every one it created, whatever the test did.

    Deletion is refused while a sandbox runs, so each is suspended first. A sandbox that will not go
    fails the teardown rather than being swallowed: a leaked one holds a PVC and a node slot. A run
    killed outright still leaks — see the sweeper entry in this package's README.
    """
    created: list[tuple[str, UUID]] = []

    async def create(
        name: str,
        *,
        template: str | None = None,
        policies: list[str] | None = None,
        session_defaults: SessionDefaults | None = None,
        bootstrap: str = "",
    ) -> SandboxView:
        if template is None:
            template = POD_TEMPLATE
        view = await client.create_sandbox(
            NewSandbox(
                name=name,
                template=template,
                policy_sets=policies or [],
                session_defaults=session_defaults,
                bootstrap=bootstrap,
            )
        )
        created.append((view.name, view.uid))
        return await wait_until_running(client, view.name)

    yield create

    for name, uid in created:
        await suspend_and_delete(client, name, uid)
