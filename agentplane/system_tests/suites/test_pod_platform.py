"""Pod workload trust and Egress credentials with native fixture identities.

The trust and route checks need no Session. The Egress credential cases use
Environment-created Sandbox descriptors and exercise the real controller and proxy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio

from agentplane.system_tests.driver.action_cli import CLI
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.native_auth import NATIVE_CLIENT_SVID_SECRET
from agentplane.system_tests.driver.native_platform import NativePlatformClients, native_platform_clients
from agentplane.system_tests.driver.native_pod_pool import Borrow, shared_pod
from agentplane.system_tests.driver.native_sandbox import native_sandbox_scope
from agentplane.system_tests.driver.policy_objects import PolicyObjects
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE, Sandboxes
from agentplane.system_tests.scenarios import cert_manager_reissue, egress_native, workload_trust

# gazelle:include_dep //agentplane/system_tests/deployment:pytest_plugin
pytest_plugins = ("agentplane.system_tests.deployment.pytest_plugin",)
pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def native_platform(cluster: Cluster) -> AsyncIterator[NativePlatformClients]:
    async with native_platform_clients(cluster) as clients:
        yield clients


@pytest_asyncio.fixture(loop_scope="module")
async def native_sandbox(cluster: Cluster, native_platform: NativePlatformClients) -> AsyncIterator[Sandboxes]:
    async with native_sandbox_scope(cluster, native_platform) as create:
        yield create


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def native_trust_pod(cluster: Cluster, native_platform: NativePlatformClients) -> AsyncIterator[Borrow]:
    async with shared_pod(cluster, native_platform) as borrow:
        yield borrow


async def test_a_cluster_ca_leaf_with_a_live_sandbox_uri_is_refused(cluster: Cluster, native_trust_pod: Borrow) -> None:
    async with native_trust_pod() as view:
        await workload_trust.a_cluster_ca_leaf_with_a_live_sandbox_uri_is_refused(cluster, view)


async def test_a_cluster_ca_leaf_naming_the_proxy_is_refused_by_the_runner(
    cluster: Cluster, native_trust_pod: Borrow
) -> None:
    async with native_trust_pod() as view:
        await workload_trust.a_cluster_ca_leaf_naming_the_proxy_is_refused_by_the_runner(
            cluster, view, route=workload_trust.RunnerControlRoute.PORT_FORWARD
        )


async def test_a_cluster_ca_leaf_at_a_runner_address_is_refused_by_runtime_proxy(
    cluster: Cluster, native_trust_pod: Borrow
) -> None:
    async with native_trust_pod() as view:
        await workload_trust.a_cluster_ca_leaf_at_a_runner_address_is_refused_by_runtime_proxy(cluster, view)


async def test_pod_cli_reaches_sessions_and_environments(cluster: Cluster, native_trust_pod: Borrow) -> None:
    async with native_trust_pod() as view:
        pod = view.kubernetes_diagnostics.pod.name
        args = ("exec", pod, "-c", "environment-runtime", "--", CLI)
        await cluster.kubectl(*args, "session-dispatcher", "list", "--limit", "1")
        await cluster.kubectl(*args, "environments", "list", "--limit", "1")


async def test_cert_manager_reissues_a_deleted_tls_secret(cluster: Cluster) -> None:
    await cert_manager_reissue.cert_manager_reissues_a_deleted_tls_secret(cluster)


async def test_a_command_box_presents_its_own_svid(cluster: Cluster, native_sandbox: Sandboxes) -> None:
    view = await native_sandbox(f"systest-egress-box-{uuid4().hex[:8]}", template=POD_TEMPLATE)
    async with PolicyObjects(cluster=cluster) as policy_objects:
        await egress_native.a_command_box_presents_its_own_svid(cluster, view, policy_objects)


async def test_a_standing_workload_authenticates_by_its_svid_and_its_prune_revokes(
    cluster: Cluster, native_sandbox: Sandboxes
) -> None:
    view = await native_sandbox(f"systest-egress-standing-{uuid4().hex[:8]}", template=POD_TEMPLATE)
    await egress_native.a_standing_workload_authenticates_by_its_svid_and_its_prune_revokes(cluster, view)


async def test_two_sandboxes_act_as_one_kubernetes_account_and_one_loses_it(
    cluster: Cluster, native_sandbox: Sandboxes
) -> None:
    await egress_native.two_sandboxes_act_as_one_kubernetes_account_and_one_loses_it(
        cluster, native_sandbox, reader_svid_secret=NATIVE_CLIENT_SVID_SECRET
    )
