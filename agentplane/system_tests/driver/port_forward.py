"""Owned kubectl port-forward process for system-test fixtures."""

import asyncio
import logging
import re
import subprocess
import threading
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from typing import Self

logger = logging.getLogger(__name__)


class PortForwardError(RuntimeError):
    """kubectl failed to report a ready port-forward listener."""


class PortForward:
    """Run a port-forward command until the sync context exits.

    ``command_prefix`` includes ``kubectl port-forward`` and any namespace or kubeconfig flags.
    Kubectl gets the resource, local-to-remote port mapping, and listen address after it.
    ``local_port=None`` delegates local port allocation to kubectl.
    """

    def __init__(
        self,
        command_prefix: Sequence[str],
        *,
        resource: str,
        local_address: str,
        local_port: int | None,
        remote_port: int,
        readiness_timeout_s: float = 30,
        termination_timeout_s: float = 5,
        output: Callable[[str], object] | None = None,
    ) -> None:
        local = "" if local_port is None else str(local_port)
        self.command = (*command_prefix, resource, f"{local}:{remote_port}", f"--address={local_address}")
        self.local_address = local_address
        self.local_port = local_port
        self.readiness_timeout_s = readiness_timeout_s
        self.termination_timeout_s = termination_timeout_s
        self.output = output
        self.process: subprocess.Popen[str] | None = None
        self._ready = threading.Event()
        self._line: str | None = None
        self._error: str | None = None
        self._cancelled = False
        self._lock = threading.Lock()
        self._tail: deque[str] = deque(maxlen=20)
        self._drainer: threading.Thread | None = None

    @property
    def endpoint(self) -> str:
        if self.local_port is None:
            raise RuntimeError("port-forward endpoint requested before readiness")
        return f"{self.local_address}:{self.local_port}"

    def __enter__(self) -> Self:
        process = subprocess.Popen(self.command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        with self._lock:
            self.process = process
            cancelled = self._cancelled
        if cancelled:
            self._terminate(process)
        self._drainer = threading.Thread(target=self._drain_output, name="port-forward-output", daemon=True)
        self._drainer.start()

        try:
            if not self._ready.wait(self.readiness_timeout_s):
                raise PortForwardError(self._failure("timed out waiting for readiness"))
            with self._lock:
                cancelled, line, error = self._cancelled, self._line, self._error
            if cancelled:
                raise PortForwardError("port-forward startup cancelled")
            if error is not None:
                raise PortForwardError(self._failure(error))
            if line is None:
                raise PortForwardError(self._failure("ended before reporting readiness"))
            if process.poll() is not None:
                raise PortForwardError(self._failure(f"exited with status {process.returncode}"))
        except BaseException:
            self._close()
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._close()

    def _cancel_startup(self) -> None:
        """Wake a pending async startup and stop the child even if Popen just completed."""
        with self._lock:
            self._cancelled = True
            process = self.process
        self._ready.set()
        if process is not None:
            self._terminate(process)

    def _close(self) -> None:
        process = self.process
        if process is not None:
            self._terminate(process)
            try:
                process.wait(timeout=self.termination_timeout_s)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

        drainer = self._drainer
        if drainer is not None and drainer is not threading.current_thread():
            drainer.join(timeout=self.termination_timeout_s)
            if drainer.is_alive():
                raise RuntimeError("port-forward output drainer remained blocked after kubectl exited")
        if process is not None and process.stdout is not None:
            process.stdout.close()

    def _drain_output(self) -> None:
        process = self.process
        assert process is not None
        stdout = process.stdout
        assert stdout is not None
        readiness = re.compile(r"^Forwarding from (\S+):(\d+) -> ")
        try:
            for line in stdout:
                with self._lock:
                    self._tail.append(line.rstrip())
                    match = readiness.match(line)
                    if match is not None:
                        address, port = match.groups()
                        expected = f"[{self.local_address}]" if ":" in self.local_address else self.local_address
                        if address == expected and self._line is None:
                            self.local_port = int(port)
                            self._line = line.rstrip()
                            self._ready.set()
                if self.output is not None:
                    try:
                        self.output(line)
                    except Exception:
                        # A failed log sink must not prevent draining kubectl's pipe.
                        logger.warning("port-forward output sink failed; continuing to drain", exc_info=True)
                        self.output = None
        except OSError as exc:
            with self._lock:
                self._error = f"could not read kubectl output: {exc}"
        finally:
            with self._lock:
                if self._line is None and self._error is None:
                    self._error = "kubectl output closed"
                self._ready.set()

    def _failure(self, reason: str) -> str:
        with self._lock:
            tail = "\n".join(self._tail)
        return f"kubectl port-forward {reason}" + (f"\nrecent output:\n{tail}" if tail else "")

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            with suppress(ProcessLookupError):
                process.terminate()


async def _wait_for_task[T](task: asyncio.Task[T]) -> None:
    """Join cleanup despite cancellation, then preserve any cancellation received meanwhile."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


@asynccontextmanager
async def async_port_forward(forward: PortForward) -> AsyncIterator[str]:
    """Async wrapper around the same process, output-drain and termination lifecycle."""
    startup = asyncio.create_task(asyncio.to_thread(forward.__enter__))
    try:
        await asyncio.shield(startup)
    except asyncio.CancelledError:
        forward._cancel_startup()
        try:
            await _wait_for_task(startup)
        except BaseException:
            logger.warning("port-forward startup failed while cancellation was pending", exc_info=True)
        cleanup = asyncio.create_task(asyncio.to_thread(forward._close))
        try:
            await _wait_for_task(cleanup)
        except BaseException:
            logger.warning("port-forward cleanup failed while cancellation was pending", exc_info=True)
        raise

    try:
        yield forward.endpoint
    finally:
        cleanup = asyncio.create_task(asyncio.to_thread(forward._close))
        await _wait_for_task(cleanup)
