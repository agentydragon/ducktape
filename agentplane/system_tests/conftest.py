"""Fixtures for the system tests: a client against a deployed app, both harnesses, and
sandboxes that clean up after themselves.

Which instance is under test comes from the environment: a scenario's target publishes a k3s stack of
its own (`agentplane.system_tests.k3s.system_test_target`). The live checks that need what only a deployment
has (`test_thread_latency`, `test_ollama_routes`) default to staging.

So does the model backend (`MODEL_BACKEND`): a real model, the default, or one the scenario scripts
(`scripted_upstream`). A scenario that needs one marks it, and the other target deselects it.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from contextlib import aclosing
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

import pytest
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.app.sandbox_models import NewSandbox, SandboxView, sandbox_is_ready
from agentplane.app.testing.client import Client
from agentplane.proto import runner_pb2
from agentplane.runner.harness import Harness
from agentplane.sandbox_resource import OperatingMode
from agentplane.system_tests import scripted_upstream
from agentplane.system_tests.ollama_warmup import warm_ollama
from util.net import Listener

# `runner_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf

BASE_URL = "AGENTPLANE_SYSTEM_TEST_URL"
TOKEN = "AGENTPLANE_SYSTEM_TEST_TOKEN"
NAMESPACE = "AGENTPLANE_SYSTEM_TEST_NAMESPACE"
SERVICE_ACCOUNT = "AGENTPLANE_SYSTEM_TEST_SERVICE_ACCOUNT"
MODEL_BACKEND = "AGENTPLANE_SYSTEM_TEST_MODEL_BACKEND"
# The standalone MCP edge's endpoint, which a k3s stack with the MCP fixtures publishes.
MCP_EDGE_URL = "AGENTPLANE_SYSTEM_TEST_MCP_EDGE_URL"


class ModelBackend(StrEnum):
    """Also the marker a scenario names its backend with."""

    REAL_MODEL = "real_model"
    SCRIPTED_MODEL = "scripted_model"


STAGING_URL = "https://agentplane-staging.allegedly.works"
STAGING_NAMESPACE = "agentplane-staging"
# The app's agent identity in every deployment (cluster/cdk8s/agentplane/app.py).
AGENT_SERVICE_ACCOUNT = "agentplane-agent"
AUDIENCE = "agentplane"

# The Pod SandboxTemplate every deployment offers, and the VM one of a deployment that runs VM Sandboxes.
POD_TEMPLATE = "runner"
VM_TEMPLATE = "runner-vm"
# A VM Sandbox's working directory: the guest's runner takes only a child of its workspace root.
VM_WORKING_DIRECTORY = "/state/workspaces/accept"

# A sandbox's first Pod has to be scheduled and pull the runner image.
SANDBOX_READY_SECONDS = 300.0

# What the `sandbox` fixture yields: create a sandbox by name and wait until it runs.
Sandboxes = Callable[..., Coroutine[Any, Any, SandboxView]]


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


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "vm_sandboxes: needs a deployment that runs VM Sandboxes")
    config.addinivalue_line("markers", "real_model: its evidence is what a real model does")
    config.addinivalue_line("markers", "scripted_model: the scenario plays the model (`scripted` fixture)")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep the scenarios this target's model backend serves; unmarked ones need no model."""
    backend = ModelBackend(os.environ.get(MODEL_BACKEND, ModelBackend.REAL_MODEL))
    other = [item for item in items if any(item.get_closest_marker(b) for b in ModelBackend if b is not backend)]
    config.hook.pytest_deselected(items=other)
    items[:] = [item for item in items if item not in other]


def namespace() -> str:
    """Read at call time: the k3s target publishes its stack after the scenario modules are imported."""
    return os.environ.get(NAMESPACE, STAGING_NAMESPACE)


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ.get(BASE_URL, STAGING_URL)


@pytest.fixture(scope="session")
def token() -> str:
    """The caller's own token if it set one, else one minted from the caller's kubeconfig."""
    if (supplied := os.environ.get(TOKEN)) is not None:
        return supplied
    return mint_token(
        namespace=namespace(), service_account=os.environ.get(SERVICE_ACCOUNT, AGENT_SERVICE_ACCOUNT), audience=AUDIENCE
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
async def offered_model(client: Client, harness: runner_pb2.Harness) -> str:
    """A model this deployment offers for this harness, asked of the app rather than hardcoded."""
    catalog = await client.models()
    config_harness = Harness(runner_pb2.Harness.Name(harness))
    offered = catalog.harnesses[config_harness]
    assert offered, f"the deployment offers no model for {harness}"
    return offered[0]


@pytest.fixture
async def model(offered_model: str) -> str:
    """`offered_model`, loaded first if Ollama serves it: a real model's turn does not wait out a cold load."""
    await warm_ollama(offered_model)
    return offered_model


@pytest.fixture
async def scripted(scripted_listener: Listener) -> AsyncIterator[scripted_upstream.ScriptedUpstream]:
    """The model behind this target's LLM ingress, played by the scenario. One per test, so a request
    a scenario leaves unanswered fails that scenario."""
    upstream = scripted_upstream.ScriptedUpstream()
    async with upstream.serving(listener=scripted_listener.socket):
        yield upstream


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
        preset: str | None = None,
        bootstrap: str = "",
        inbox_tags: Sequence[str] = (),
    ) -> SandboxView:
        if template is None:
            template = POD_TEMPLATE
        view = await client.create_sandbox(
            NewSandbox(
                name=name,
                template=template,
                preset=preset,
                policy_sets=policies or [],
                inbox_tags=list(inbox_tags),
                bootstrap=bootstrap,
            )
        )
        created.append((view.name, view.uid))
        return await wait_until_running(client, view.name)

    yield create

    for name, uid in created:
        await suspend_and_delete(client, name, uid)
