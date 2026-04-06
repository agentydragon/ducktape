"""Shared fixtures for AppImage-based FreeCAD tests."""

import os
import subprocess
from pathlib import Path

import pytest

from util.bazel.runfiles import get_required_path

_FREECAD_RLOC = "_main/skills/freecad/explore_appimage/freecad_appimage.rloc"


@pytest.fixture(scope="session")
def freecad_run():
    """Run a FreeCAD script headlessly via the AppImage and return CompletedProcess."""
    rloc_file = get_required_path(_FREECAD_RLOC)
    appimage = get_required_path(rloc_file.read_text().strip())

    def _run(script: Path, outdir: Path, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(appimage), "freecadcmd", str(script)],
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "OUTDIR": str(outdir)},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    return _run
