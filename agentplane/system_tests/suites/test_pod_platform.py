"""Pod workload trust and Egress credentials with native fixture identities.

The four workload-trust and certificate cases need no Session. The three Egress credential cases
use Environment-created Sandbox descriptors and retain their real controller and proxy checks.
"""

from __future__ import annotations

from uuid import uuid4

import pytest_bazel

from agentplane.environments.v1 import environment_service_pb2
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.native_auth import NATIVE_CLIENT_NAME
from agentplane.system_tests.driver.native_platform import NativePlatformClients
from agentplane.system_tests.driver.policy_objects import PolicyObjects
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE, Sandboxes
from agentplane.system_tests.scenarios import cert_manager_reissue, egress_native, workload_trust

# `environment_service_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf


# gazelle:include_dep //agentplane/system_tests/deployment:pytest_plugin
# gazelle:include_dep //agentplane/system_tests/driver:native_fixtures
pytest_plugins = ("agentplane.system_tests.deployment.pytest_plugin", "agentplane.system_tests.driver.native_fixtures")


async def _ready_pod(
    native_platform: NativePlatformClients, native_sandbox: Sandboxes
) -> environment_service_pb2.EnvironmentDescriptor:
    view = await native_sandbox(f"systest-trust-native-{uuid4().hex[:8]}", template=POD_TEMPLATE)
    assert view.kubernetes_diagnostics.HasField("pod")
    observed = await native_platform.environment.get_environment(view.target)
    assert observed.target == view.target
    assert observed.kubernetes_diagnostics.HasField("pod")
    assert observed.kubernetes_diagnostics.pod.uid == view.kubernetes_diagnostics.pod.uid
    return view


async def test_a_cluster_ca_leaf_with_a_live_sandbox_uri_is_refused(
    cluster: Cluster, native_platform: NativePlatformClients, native_sandbox: Sandboxes
) -> None:
    view = await _ready_pod(native_platform, native_sandbox)
    await workload_trust.a_cluster_ca_leaf_with_a_live_sandbox_uri_is_refused(cluster, view)


async def test_a_cluster_ca_leaf_naming_the_gateway_is_refused_by_the_runner(
    cluster: Cluster, native_platform: NativePlatformClients, native_sandbox: Sandboxes
) -> None:
    view = await _ready_pod(native_platform, native_sandbox)
    await workload_trust.a_cluster_ca_leaf_naming_the_gateway_is_refused_by_the_runner(
        cluster, view, route=workload_trust.RunnerControlRoute.PORT_FORWARD
    )


async def test_a_cluster_ca_leaf_at_a_runner_address_is_refused_by_the_gateway(
    cluster: Cluster, native_platform: NativePlatformClients, native_sandbox: Sandboxes
) -> None:
    view = await _ready_pod(native_platform, native_sandbox)
    await workload_trust.a_cluster_ca_leaf_at_a_runner_address_is_refused_by_the_gateway(cluster, view)


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
        cluster, native_sandbox, reader_service_account=NATIVE_CLIENT_NAME
    )


if __name__ == "__main__":
    pytest_bazel.main()
