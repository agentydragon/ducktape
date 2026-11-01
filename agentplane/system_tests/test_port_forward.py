"""Lifecycle regressions for the system-test kubectl port-forward helper."""

import asyncio
import sys
import threading
from collections.abc import Callable

import pytest
import pytest_bazel

from agentplane.system_tests.port_forward import PortForward, PortForwardError, async_port_forward


def _forward(
    script: str, *, readiness_timeout_s: float = 30, output: Callable[[str], object] | None = None
) -> PortForward:
    return PortForward(
        (sys.executable, "-u", "-c", script),
        resource="service/test-service",
        local_address="127.0.0.1",
        local_port=None,
        remote_port=8080,
        readiness_timeout_s=readiness_timeout_s,
        output=output,
    )


class _SignalOutput:
    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.seen = threading.Event()

    def __call__(self, text: str) -> None:
        if self.marker in text:
            self.seen.set()


def _ready_script(port: int = 23456) -> str:
    return f"import signal; print('Forwarding from 127.0.0.1:{port} -> 8080', flush=True); signal.pause()"


def _assert_stopped(forward: PortForward) -> None:
    process = forward.process
    assert process is not None
    assert process.returncode is not None


def test_command_preserves_fixed_local_and_remote_ports() -> None:
    forward = PortForward(
        ("kubectl", "port-forward", "-n", "test-namespace"),
        resource="service/test-service",
        local_address="127.0.0.1",
        local_port=18080,
        remote_port=8080,
    )

    assert forward.command == (
        "kubectl",
        "port-forward",
        "-n",
        "test-namespace",
        "service/test-service",
        "18080:8080",
        "--address=127.0.0.1",
    )


def test_readiness_parses_allocated_port_and_drains_output_for_full_lifetime() -> None:
    sink = _SignalOutput("DRAINED")
    script = (
        "import signal; "
        "print('Forwarding from 127.0.0.1:23456 -> 8080', flush=True); "
        "[print('connection output ' + str(i) + 'x' * 100, flush=True) for i in range(10000)]; "
        "print('DRAINED', flush=True); signal.pause()"
    )
    forward = _forward(script, output=sink)

    with forward:
        assert forward.endpoint == "127.0.0.1:23456"
        assert sink.seen.wait(5), "port-forward output pipe stopped draining after readiness"

    _assert_stopped(forward)
    drainer = forward._drainer
    assert drainer is not None
    assert not drainer.is_alive()


def test_process_exit_before_readiness_reports_output_and_reaps_process() -> None:
    forward = _forward("import sys; print('service is unavailable', flush=True); sys.exit(17)")

    with pytest.raises(PortForwardError, match="service is unavailable"):
        forward.__enter__()

    process = forward.process
    assert process is not None
    assert process.returncode == 17


def test_readiness_timeout_terminates_process() -> None:
    forward = _forward("import signal; signal.pause()", readiness_timeout_s=0.05)

    with pytest.raises(PortForwardError, match="timed out waiting for readiness"):
        forward.__enter__()

    _assert_stopped(forward)


def test_body_error_still_stops_process() -> None:
    forward = _forward(_ready_script())

    with pytest.raises(ValueError, match="scenario failed"), forward:
        raise ValueError("scenario failed")

    _assert_stopped(forward)


async def _cancel_async_context_during_body() -> None:
    forward = _forward(_ready_script())
    entered = asyncio.Event()

    async def use_forward() -> None:
        async with async_port_forward(forward):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(use_forward())
    async with asyncio.timeout(5):
        await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _assert_stopped(forward)


def test_async_body_cancellation_stops_process() -> None:
    asyncio.run(_cancel_async_context_during_body())


async def _async_context_normal_exit() -> None:
    forward = _forward(_ready_script())
    async with async_port_forward(forward) as endpoint:
        assert endpoint == "127.0.0.1:23456"
    _assert_stopped(forward)


def test_async_normal_exit_stops_process() -> None:
    asyncio.run(_async_context_normal_exit())


async def _cancel_async_startup() -> None:
    sink = _SignalOutput("CHILD_STARTED")
    forward = _forward("import signal; print('CHILD_STARTED', flush=True); signal.pause()", output=sink)

    async def use_forward() -> None:
        async with async_port_forward(forward):
            raise AssertionError("a process with no readiness line must not enter the context")

    task = asyncio.create_task(use_forward())
    assert await asyncio.to_thread(sink.seen.wait, 5), "child did not start"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(5):
            await task
    _assert_stopped(forward)


def test_async_startup_cancellation_stops_process() -> None:
    asyncio.run(_cancel_async_startup())


if __name__ == "__main__":
    pytest_bazel.main()
