"""Shared fixtures for AppImage-based FreeCAD tests."""

import os
import subprocess
from pathlib import Path

import pytest

from util.bazel.runfiles import get_required_path

# Rloc file written by freecad_appimage_rloc rule; contains the rlocation
# of squashfs-root/usr/bin/freecadcmd within the @freecad_appimage repo.
_FREECAD_RLOC = "_main/skills/freecad/explore_appimage/freecad_appimage.rloc"


@pytest.fixture(scope="session")
def freecad_squashfs_root() -> Path:
    """Absolute path to the extracted AppImage squashfs-root directory."""
    rloc_file = get_required_path(_FREECAD_RLOC)
    freecadcmd_rloc = rloc_file.read_text().strip()
    freecadcmd = get_required_path(freecadcmd_rloc)
    # squashfs-root/usr/bin/freecadcmd -> parents: bin, usr, squashfs-root
    return freecadcmd.parents[2]


@pytest.fixture(scope="session")
def freecad_run(freecad_squashfs_root: Path):
    """Run a FreeCAD script headlessly and return the CompletedProcess."""
    prefix = freecad_squashfs_root / "usr"
    freecadcmd = prefix / "bin" / "freecadcmd"

    base_env = {
        **os.environ,
        "PREFIX": str(prefix),
        "PYTHONHOME": str(prefix),
        "PATH_TO_FREECAD_LIBDIR": str(prefix / "lib"),
        "SSL_CERT_FILE": str(prefix / "ssl" / "cacert.pem"),
        "QT_QPA_PLATFORM": "offscreen",
        # Prepend FreeCAD's bin so its Python and tools take precedence
        "PATH": str(prefix / "bin") + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
    }

    def _run(script: Path, outdir: Path, timeout: int = 60) -> subprocess.CompletedProcess:
        """Run freecadcmd <script> with OUTDIR set to outdir."""
        return subprocess.run(
            [str(freecadcmd), str(script)],
            env={**base_env, "OUTDIR": str(outdir)},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    return _run
