"""Build information parsed from Bazel workspace status at runtime."""

from __future__ import annotations

from importlib import resources


def get_build_commit() -> str:
    """Read build commit from workspace status file.

    Returns the STABLE_BUILD_COMMIT value from the status file,
    or "dev" if not available (e.g., running outside Bazel).
    """
    try:
        # Read the status file from package data
        status_text = resources.files(__package__).joinpath("_build_status.txt").read_text()
        for line in status_text.splitlines():
            if line.startswith("STABLE_BUILD_COMMIT "):
                return line.split(" ", 1)[1]
    except FileNotFoundError:
        # Expected when running outside Bazel (e.g., development)
        pass
    return "dev"


# For backwards compatibility and simple imports
BUILD_COMMIT = get_build_commit()
