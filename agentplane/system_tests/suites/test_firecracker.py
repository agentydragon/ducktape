"""Hardware-backed Firecracker acceptance on the dedicated BuildBuddy KVM worker."""

from uuid import uuid4

import pytest
import pytest_bazel

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.native_platform import NativePlatformClients
from agentplane.system_tests.driver.sandbox import Sandboxes
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.scenarios import workload_trust
from agentplane.system_tests.scenarios.firecracker import preserves_live_guest

# gazelle:include_dep //agentplane/system_tests/firecracker:pytest_plugin
# gazelle:include_dep //agentplane/system_tests/driver:native_fixtures
pytest_plugins = ("agentplane.system_tests.firecracker.pytest_plugin", "agentplane.system_tests.driver.native_fixtures")


@pytest.mark.scripted_model
async def test_firecracker_preserves_live_guest(
    cluster: Cluster, native_platform: NativePlatformClients, native_sandbox: Sandboxes, scripted: ScriptedUpstream
) -> None:
    await preserves_live_guest(cluster, native_platform, native_sandbox, scripted)


async def test_a_cluster_ca_leaf_naming_the_proxy_is_refused_by_the_guest_runner(
    cluster: Cluster, native_sandbox: Sandboxes
) -> None:
    view = await native_sandbox(f"systest-firecracker-trust-{uuid4().hex[:8]}", template="environment-firecracker")
    await workload_trust.a_cluster_ca_leaf_naming_the_proxy_is_refused_by_the_runner(
        cluster, view, route=workload_trust.RunnerControlRoute.POD_IP
    )


if __name__ == "__main__":
    pytest_bazel.main()
