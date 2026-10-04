"""Shell utilities for command emission and error handling."""

import errno
import os


def emit_command(cmd: str) -> None:
    """Emit a command for shell execution via fd3."""
    # fd3 not available (e.g., in tests or non-shell environments)
    # Only ignore specific, expected errno values; re-raise others
    try:
        os.write(3, (cmd + "\n").encode())
    except OSError as e:
        # Ignore expected cases when fd 3 is unavailable in tests/non-shell envs
        if e.errno in (errno.EBADF, errno.EINVAL, errno.ENXIO):  # not open/invalid/device
            return
        raise
