"""Boot one real CI test guest and exercise its ordinary ACPI shutdown path."""

import pytest
import pytest_bazel
from opentelemetry import trace

from agentplane.app.testing.client import Client
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.sandbox import VM_TEMPLATE, sandbox_scope
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

    write_progress(output_dir, "sandbox_deleted", sandbox_uid=sandbox.target.uid)


if __name__ == "__main__":
    pytest_bazel.main()
