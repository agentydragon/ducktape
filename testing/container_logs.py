"""Testcontainers subclass that streams container logs to undeclared test outputs.

Logs are written incrementally by a background thread, so they survive
Bazel test timeouts (SIGTERM/SIGKILL) — whatever was flushed to disk
before the kill is preserved in the undeclared outputs.
"""

import threading
from pathlib import Path
from types import TracebackType

from docker.models.containers import Container
from testcontainers.core.container import DockerContainer

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


def stream_container_logs(container: Container, path: Path) -> threading.Thread:
    """Follow stdout and stderr to disk. Join the returned thread after stopping the container."""
    path.parent.mkdir(parents=True, exist_ok=True)
    output = path.open("wb")

    def stream() -> None:
        with output:
            for chunk in container.logs(stream=True, follow=True, stdout=True, stderr=True):
                output.write(chunk)
                output.flush()

    thread = threading.Thread(target=stream, daemon=True)
    thread.start()
    return thread


class LoggedContainer(DockerContainer):
    """DockerContainer that streams stdout/stderr to undeclared test outputs.

    Logs are written continuously by a background thread started on __enter__,
    so partial output is available even if the test process is killed.
    """

    def __init__(self, *args, test_name: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._test_name = test_name
        self._log_thread: threading.Thread | None = None

    def __enter__(self):
        result = super().__enter__()
        path = undeclared_outputs_dir() / self._test_name / "container.log"
        self._log_thread = stream_container_logs(self.get_wrapped_container(), path)
        return result

    def __exit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None
    ) -> None:
        super().__exit__(exc_type, exc_val, exc_tb)
        # After stop() kills the container, the log stream ends and the thread exits.
        if self._log_thread:
            self._log_thread.join(timeout=3)
