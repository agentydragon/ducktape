"""Run the aiquota server with the frontend bundle declared by its Bazel target."""

from __future__ import annotations

import os
from pathlib import Path

from aiquota.api import main as run_server
from devinfra.bazel.runfiles import get_required_path

_FRONTEND_RLOCATION_ENV = "AIQUOTA_FRONTEND_RLOCATION"


def _frontend_dir() -> Path:
    rlocation = os.environ.get(_FRONTEND_RLOCATION_ENV)
    if not rlocation:
        raise RuntimeError(f"{_FRONTEND_RLOCATION_ENV} must be set by the server's Bazel target")
    frontend_dir = get_required_path(rlocation)
    if not (frontend_dir / "index.html").is_file():
        raise RuntimeError(f"aiquota frontend bundle at {rlocation!r} has no index.html")
    return frontend_dir


def main() -> None:
    run_server(frontend_dir=_frontend_dir())


if __name__ == "__main__":
    main()
