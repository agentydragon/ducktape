"""Statically selected VM lifecycle and guest-console access for VM suites."""

from collections.abc import Iterator

import pytest

from agentplane.system_tests.deployment.virtualization import VirtualizationFactory
from agentplane.system_tests.vm import guest_logs
from agentplane.system_tests.vm.runtime import VmRuntime
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


@pytest.fixture(scope="session")
def virtualization_factory() -> Iterator[VirtualizationFactory]:
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv(guest_logs.CONSOLES_ENV, str(undeclared_outputs_dir() / "vm-artifacts/consoles"))
        yield VmRuntime
