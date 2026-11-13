"""One real VM boot and its Runtime/Gateway credential path, without a harness turn."""

from __future__ import annotations

import pytest
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import session_pb2
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.sandbox import VM_TEMPLATE, Sandboxes
from agentplane.system_tests.scenarios import runner_credentials

# session_pb2.pyi imports protobuf stubs directly during mypy's dependency traversal.
# gazelle:include_dep @pypi//protobuf

pytestmark = pytest.mark.vm_sandboxes


async def test_vm_boot_contract(client: Client, cluster: Cluster, sandbox: Sandboxes) -> None:
    view = await sandbox("systest-vm-boot-contract", template=VM_TEMPLATE)

    assert view.observed_state.operating_mode == environment_service_pb2.OPERATING_MODE_RUNNING
    assert view.observed_state.phase == environment_service_pb2.SANDBOX_PHASE_RUNNING
    assert view.observed_state.readiness.state == environment_service_pb2.READINESS_STATE_READY
    assert view.kubernetes_diagnostics.HasField("pod")
    assert view.kubernetes_diagnostics.pod.uid

    pod_uid, credential_id = await runner_credentials.check_vm_runner_certificate(cluster, view)
    assert pod_uid == view.kubernetes_diagnostics.pod.uid
    assert credential_id

    # This typed public app endpoint calls GetStatus through Runner Gateway to the VM Runtime,
    # proving both services are reachable over their authenticated transport after image boot.
    runtime = await client.runner_status(view.target.name, view.target.uid)
    assert runtime.environment_runtime_boot_id
    assert runtime.HasField("lifecycle")
    assert runtime.lifecycle.desired_mode == session_pb2.ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_RUNNING
    assert runtime.lifecycle.progress_available
    assert runtime.lifecycle.progress_current
    assert runtime.lifecycle.stage == session_pb2.ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_RUNNING
    assert not runtime.lifecycle.error
    assert not runtime.lifecycle.HasField("blocker")


if __name__ == "__main__":
    pytest_bazel.main()
