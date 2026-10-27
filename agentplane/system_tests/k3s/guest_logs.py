"""Retain VM shutdown evidence before kubelet removes the container logs."""

import logging
import os
import shutil
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)
SHUTDOWN_COMPLETE = b"AGENTPLANE_GUEST_SHUTDOWN_COMPLETE"


class GuestShutdownError(RuntimeError):
    """A retained guest incarnation did not complete its orderly shutdown."""


def _copy(log_root: Path, scopes: dict[str, Path], stop: threading.Event) -> None:
    # Open descriptors keep the last bytes readable after suspend/delete unlinks the source.
    opened: set[tuple[int, int]] = set()
    streams: list[tuple[BinaryIO, BinaryIO]] = []
    with ExitStack() as files:
        while True:
            for pattern, destination in scopes.items():
                for path in log_root.glob(pattern):
                    _retain(path, destination, opened, streams, files)
            for reader, writer in streams:
                shutil.copyfileobj(reader, writer)
                writer.flush()
            if stop.is_set():
                return
            stop.wait(0.1)


def _retain(
    path: Path,
    destination: Path,
    opened: set[tuple[int, int]],
    streams: list[tuple[BinaryIO, BinaryIO]],
    files: ExitStack,
) -> None:
    try:
        source = path.open("rb")
    except FileNotFoundError:
        # kubelet may remove a launcher between directory enumeration and open.
        return
    with source:
        stat = os.fstat(source.fileno())
        identity = (stat.st_dev, stat.st_ino)
        if identity in opened:
            return
        target = destination / path.parent.parent.name / f"{path.name}-{identity[1]}.cri.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        # dup the open descriptor, retaining the inode even if its pathname disappears.
        retained = files.enter_context(os.fdopen(os.dup(source.fileno()), "rb"))
        streams.append((retained, files.enter_context(target.open("wb"))))
        opened.add(identity)


def _require_shutdown(consoles: Path) -> None:
    # Log rotation/container restarts can split one launcher's evidence across several files.
    missing = sorted(
        incarnation.name
        for incarnation in consoles.glob("*")
        if incarnation.is_dir()
        and not any(
            line.split(b" ", 3)[1:] == [b"stdout", b"F", SHUTDOWN_COMPLETE]
            for path in incarnation.glob("*.cri.log")
            for line in path.read_bytes().splitlines()
        )
    )
    if missing:
        raise GuestShutdownError(f"Guest shutdown did not complete for launcher(s): {', '.join(missing)}")


@contextmanager
def capture(
    *, log_root: Path, namespace: str, kubevirt_namespace: str, destination: Path, validate_shutdown: Callable[[], bool]
) -> Iterator[None]:
    """Retain consoles, launcher compute and virt-handler logs; validate only successful teardown.

    A pytest session fixture exits normally even when its tests failed, so its caller supplies a
    teardown-time predicate in addition to the exception-unwind protection here.
    """
    scopes = {
        f"{namespace}_virt-launcher-*/guest-console-log/*.log*": destination / "consoles",
        f"{namespace}_virt-launcher-*/compute/*.log*": destination / "launchers",
        f"{kubevirt_namespace}_virt-handler-*/virt-handler/*.log*": destination / "handlers",
    }
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="vm-guest-logs") as executor:
        copying = executor.submit(_copy, log_root, scopes, stop)
        try:
            yield
        except BaseException:
            stop.set()
            try:
                copying.result()
            except Exception:
                logger.exception("Could not finish retaining VM logs while unwinding a failure")
            raise
        finally:
            stop.set()
        copying.result()
        if validate_shutdown():
            _require_shutdown(destination / "consoles")
