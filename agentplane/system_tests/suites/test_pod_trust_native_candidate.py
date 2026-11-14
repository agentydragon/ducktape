"""Four unchanged Pod scenarios on native App-free fixture identities.

The ordinary workload-trust bindings use only `Pair.claude`; they assert no second-harness
behavior. One ready Pod Sandbox descriptor is the scenario contract, plus any impostor Sandbox
created by the scenario itself.
"""

from __future__ import annotations

from uuid import uuid4

import pytest_bazel

from agentplane.environments.v1 import environment_service_pb2
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.native_platform import NativePlatformClients
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE, Sandboxes
from agentplane.system_tests.scenarios import cert_manager_reissue, workload_trust

# `environment_service_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf


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


if __name__ == "__main__":
    pytest_bazel.main()
