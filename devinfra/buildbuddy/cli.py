"""Invoke the pinned bbapi CLI from Python orchestration code."""

from __future__ import annotations

import os
import subprocess


class CLIError(Exception):
    """bbapi could not complete a request."""


def run(*args: str, timeout: float = 120, api_key: str | None = None, base_url: str | None = None) -> bytes:
    env = os.environ.copy()
    if api_key is not None:
        env["BUILDBUDDY_API_KEY"] = api_key
    if base_url is not None:
        env["BUILDBUDDY_URL"] = base_url
    try:
        result = subprocess.run(
            ["bbapi", "--timeout", f"{timeout}s", *args], env=env, capture_output=True, check=True, timeout=timeout
        )
    except subprocess.CalledProcessError as error:
        raise CLIError(error.stderr.decode(errors="replace").strip()) from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CLIError(str(error)) from error
    return result.stdout
