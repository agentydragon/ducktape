"""KubeVirt runtime fixtures for guest deployment suites."""

from collections.abc import Iterator

import pytest

from agentplane.system_tests.deployment.virtualization import RuntimeFactory
from agentplane.system_tests.vm import guest_logs
from agentplane.system_tests.vm.runtime import runtime
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


@pytest.fixture(scope="session")
def kubevirt_runtime_factory() -> Iterator[RuntimeFactory]:
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv(guest_logs.CONSOLES_ENV, str(undeclared_outputs_dir() / "vm-artifacts/consoles"))
        yield runtime
