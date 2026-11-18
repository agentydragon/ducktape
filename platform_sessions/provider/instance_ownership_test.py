"""Provider process exclusion survives abrupt owner termination."""

import os
import signal
from pathlib import Path

import pytest
from filelock import Timeout

from platform_sessions.provider.instance_ownership import InstanceOwnership


def test_crashed_owner_releases_process_lock(tmp_path: Path) -> None:
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        try:
            os.close(read_fd)
            owner = InstanceOwnership(tmp_path)
            os.write(write_fd, b"owned")
            signal.pause()
            owner.close()
        finally:
            os._exit(1)
    os.close(write_fd)
    try:
        with os.fdopen(read_fd, "rb") as pipe:
            assert pipe.read(5) == b"owned"
        with pytest.raises(Timeout):
            InstanceOwnership(tmp_path)
    finally:
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
    successor = InstanceOwnership(tmp_path)
    successor.close()
