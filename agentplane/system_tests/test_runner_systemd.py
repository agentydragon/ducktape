"""Gate the real-manager kernel proof once, independently of the VM harness scenarios."""

import pytest
import pytest_bazel
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.system_tests.conftest import VM_TEMPLATE, Sandboxes
from agentplane.system_tests.k3s.guest_logs import check_boot, systemd_proof_passed


@pytest.mark.vm_sandboxes
async def test_real_systemd_supervision(sandbox: Sandboxes) -> None:
    # Runner readiness requires the proof to finish in this target's guest. Also require its
    # positive receipt, so an accidentally selected ordinary guest cannot silently pass this test.
    await sandbox("systest-systemd-proof", template=VM_TEMPLATE)
    async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.1), reraise=True):
        with attempt:
            check_boot()
            assert systemd_proof_passed(), "Missing completed systemd proof receipt in retained guest console"


if __name__ == "__main__":
    pytest_bazel.main()
