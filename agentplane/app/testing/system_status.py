"""A `SystemStatusChecker` over probes the test scripts."""

from agentplane.app.system_status import Probe, SystemStatusChecker


async def _answers() -> None:
    pass


def answering_system_status() -> SystemStatusChecker:
    return SystemStatusChecker([Probe("Test component", _answers)], timeout_s=1)
