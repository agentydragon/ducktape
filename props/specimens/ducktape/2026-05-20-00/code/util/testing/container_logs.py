"""Testcontainers subclass that streams container logs to undeclared test outputs.

Logs are written incrementally by a background thread, so they survive
Bazel test timeouts (SIGTERM/SIGKILL) — whatever was flushed to disk
before the kill is preserved in the undeclared outputs.
"""

import threading
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

from testcontainers.core.container import DockerContainer

from util.testing.undeclared_outputs import undeclared_outputs_dir


def _stream_logs(container, out_dir: Path) -> None:
    """Stream combined container logs to disk. Runs in a daemon thread."""
    with (out_dir / "container.log").open("wb") as f:
        for chunk in container.logs(stream=True, follow=True, stdout=True, stderr=True):
            f.write(chunk)
            f.flush()


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
        out_dir = undeclared_outputs_dir() / self._test_name
        out_dir.mkdir(parents=True, exist_ok=True)
        self._log_thread = threading.Thread(target=_stream_logs, args=(self._container, out_dir), daemon=True)
        self._log_thread.start()
        return result

    def __exit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None
    ) -> None:
        super().__exit__(exc_type, exc_val, exc_tb)
        # After stop() kills the container, the log stream ends and the thread exits.
        if self._log_thread:
            self._log_thread.join(timeout=3)


LoggedContainerFactory = Callable[..., LoggedContainer]
