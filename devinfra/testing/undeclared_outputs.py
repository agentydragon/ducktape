"""Access Bazel's TEST_UNDECLARED_OUTPUTS_DIR for test artifact collection."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_ENV_VAR = "TEST_UNDECLARED_OUTPUTS_DIR"


def undeclared_outputs_dir() -> Path:
    """Return a directory for test artifacts.

    Uses Bazel's TEST_UNDECLARED_OUTPUTS_DIR when set, otherwise falls back
    to a temporary directory.
    """
    d = os.environ.get(_ENV_VAR)
    p = Path(d) if d else Path(tempfile.gettempdir()) / "test-outputs"
    p.mkdir(parents=True, exist_ok=True)
    return p
