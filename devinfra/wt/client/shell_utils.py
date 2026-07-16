"""Shell utilities for command emission and error handling."""

import errno
import os
import sys

import click


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


def controlled_error(message: str, commands: list[str] | None = None) -> None:
    """Exit with a controlled error message and optional commands."""
    click.echo(f"Error: {message}")
    if commands:
        for cmd in commands:
            emit_command(cmd)
    sys.exit(2)  # Controlled error - eval fd 3 contents
