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
