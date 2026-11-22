"""Measure overlapping Firecracker guest cleanup with the proxy-trust scenario."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import nullcontext
from uuid import uuid4

import pytest
import pytest_asyncio
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.system_tests.driver.app_sandbox import sandbox_scope
from agentplane.system_tests.driver.case_batch import Case, CaseBatch, selected_items
from agentplane.system_tests.driver.cleanup import resource_scope
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.dispatcher_transport import native_client
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.scenarios import workload_trust
from agentplane.system_tests.scenarios.firecracker import preserves_live_guest
from util.net import Listener

pytest_plugins = ("agentplane.system_tests.firecracker.pytest_plugin",)
pytestmark = [pytest.mark.case_batch, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def firecracker_cases(
    request: pytest.FixtureRequest, cluster: Cluster, base_url: str, token: str, scripted_listener: Listener
) -> AsyncIterator[CaseBatch]:
    mode = os.environ.get("AGENTPLANE_FIRECRACKER_CLEANUP_MODE", "serial")
    if mode not in {"serial", "overlap"}:
        raise pytest.UsageError("AGENTPLANE_FIRECRACKER_CLEANUP_MODE must be serial or overlap")

    start_trust = asyncio.Event()

    async def live_guest(_worker: None) -> None:
        try:
            async with resource_scope() as resources:
                dispatcher = await resources.enter_async_context(native_client(cluster))
                client = await resources.enter_async_context(
                    Client.connect(base_url=base_url, token=token, dispatcher=dispatcher)
                )
                sandbox = await resources.enter_async_context(sandbox_scope(cluster, client, tracer))
                scripted = ScriptedUpstream()
                with tracer.start_as_current_span("system_test.firecracker.live_assertions"):
                    async with scripted.serving(listener=scripted_listener.socket):
                        await preserves_live_guest(cluster, client, dispatcher, sandbox, scripted)
                if mode == "overlap":
                    start_trust.set()
        finally:
            # Serial and failed live assertions release the trust case after live cleanup.
            start_trust.set()

    async def proxy_trust(_worker: None) -> None:
        await start_trust.wait()
        with tracer.start_as_current_span("system_test.firecracker.proxy_trust"):
            async with resource_scope() as resources:
                client = await resources.enter_async_context(Client.connect(base_url=base_url, token=token))
                sandbox = await resources.enter_async_context(sandbox_scope(cluster, client, tracer))
                view = await sandbox(f"systest-firecracker-trust-{uuid4().hex[:8]}", template="environment-firecracker")
                await workload_trust.a_cluster_ca_leaf_naming_the_proxy_is_refused_by_the_runner(
                    cluster, view, route=workload_trust.RunnerControlRoute.POD_IP
                )

    callbacks = {
        "test_firecracker_preserves_live_guest": live_guest,
        "test_a_cluster_ca_leaf_naming_the_proxy_is_refused_by_the_guest_runner": proxy_trust,
    }
    items = selected_items(request)
    if not any(item.name == "test_firecracker_preserves_live_guest" for item in items):
        start_trust.set()
    cases = {item.nodeid: Case.for_item(item, callbacks[item.name]) for item in items}
    async with CaseBatch.running(cases, limit=2, worker_scope=nullcontext) as batch:
        yield batch


@pytest.mark.scripted_model
async def test_firecracker_preserves_live_guest(firecracker_cases: CaseBatch, request: pytest.FixtureRequest) -> None:
    await firecracker_cases.report(request)


async def test_a_cluster_ca_leaf_naming_the_proxy_is_refused_by_the_guest_runner(
    firecracker_cases: CaseBatch, request: pytest.FixtureRequest
) -> None:
    await firecracker_cases.report(request)


if __name__ == "__main__":
    pytest_bazel.main()
