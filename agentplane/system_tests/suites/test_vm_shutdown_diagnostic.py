"""Boot one real CI test guest and exercise its ordinary ACPI shutdown path."""

import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict
from opentelemetry import trace

from agentplane.app.testing.client import Client
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.sandbox import VM_TEMPLATE, sandbox_scope
from agentplane.system_tests.vm import host_qemu
from agentplane.system_tests.vm.shutdown_diagnostic import write_progress
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

pytestmark = pytest.mark.vm_sandboxes


@pytest.mark.timeout(600)
async def test_one_guest_receives_acpi_and_powers_down(
    cluster: Cluster, client: Client, phase_tracer: trace.Tracer
) -> None:
    output_dir = undeclared_outputs_dir()
    write_progress(output_dir, "started")
    async with sandbox_scope(cluster, client, phase_tracer) as create:
        sandbox = await create("vm-shutdown-diagnostic", template=VM_TEMPLATE)
        write_progress(output_dir, "guest_ready", sandbox_uid=sandbox.target.uid)
        if host_qemu.virtiofsd_timeline_enabled():
            if not sandbox.kubernetes_diagnostics.HasField("pod"):
                raise AssertionError("Ready VM Sandbox did not project its launcher Pod identity")
            pod = sandbox.kubernetes_diagnostics.pod
            if pod.uid != sandbox.observed_state.runtime_incarnation_uid:
                raise AssertionError(
                    "projected launcher Pod UID does not match observed runtime incarnation UID: "
                    f"pod={pod.uid!r} runtime={sandbox.observed_state.runtime_incarnation_uid!r}"
                )
            if pod.namespace != cluster.namespace or pod.deleting:
                raise AssertionError(
                    "projected launcher Pod is outside the test namespace or already deleting: "
                    f"namespace={pod.namespace!r} deleting={pod.deleting}"
                )
            # sandbox_scope already verified this exact projected Pod UID and its live VMI owner
            # through assert_admitted_launcher_topology before returning from create().
            # Collector failures are retained as diagnostics but do not alter guest behavior.
            pod_status = MessageToDict(pod.status) if pod.HasField("status") else {}
            host_qemu.record_virtiofsd_ready_baseline(
                output_dir / "vm-artifacts",
                pod.namespace,
                pod.name,
                pod.uid,
                host_qemu.virtiofs_container_names_by_id(pod_status),
            )

    write_progress(output_dir, "sandbox_deleted", sandbox_uid=sandbox.target.uid)


if __name__ == "__main__":
    pytest_bazel.main()
