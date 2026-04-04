"""Shared fixtures for FreeCAD tests."""

import pytest
import pytest_bazel

from util.oci import OciImage, load_oci_image
from util.testing.container_logs import LoggedContainer

FREECAD_TEST = OciImage("_main/skills/freecad/freecad_test.rloc", "freecad-test:pinned")


@pytest.fixture(scope="session")
def freecad_image() -> str:
    """Load FreeCAD test image into Docker daemon and return its tag."""
    return load_oci_image(FREECAD_TEST)


def freecad_exec(container: LoggedContainer, cmd: str) -> None:
    """Run a command in a FreeCAD container, asserting success."""
    result = container.exec(cmd)
    output = result.output.decode(errors="replace")
    print(output)
    assert result.exit_code == 0, f"Command failed (exit {result.exit_code}): {output[:500]}"


def freecad_setup_fonts(container: LoggedContainer) -> None:
    """Register FreeCAD's bundled osifont with fontconfig for deterministic TechDraw exports.

    CLEANUP(2026-04-05): Remove once the rebuilt Docker image (Dockerfile.test) includes
    the fc-cache + symlink step. After that, osifont is registered at image build time
    and this runtime call becomes a no-op.
    """
    result = container.exec(
        "bash -c 'ln -sf /opt/squashfs-root/usr/Mod/TechDraw/Resources/fonts"
        " /usr/local/share/fonts/techdraw && fc-cache -f 2>/dev/null'"
    )
    assert result.exit_code == 0, f"Font setup failed: {result.output.decode(errors='replace')[:200]}"
