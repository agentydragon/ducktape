"""Golden-file test: build.py -> export_page.py -> DXF/SVG/PDF."""

from pathlib import Path

import pytest
import pytest_bazel
from python.runfiles import Runfiles

from skills.freecad.conftest import Stage, StageRunner
from skills.freecad.testing.compare import assert_dxf_equal, assert_pdf_equal, assert_svg_equal

# The scripts below are driven by runfiles path; gazelle cannot see the dependency.
# gazelle:include_dep //skills/freecad/examples/compound:build
# gazelle:include_dep //skills/freecad/examples:export_page

_BUILD_SCRIPT = "ducktape/skills/freecad/examples/compound/build.py"
_EXPORT_SCRIPT = "ducktape/skills/freecad/examples/export_page.py"
_GOLDEN_DXF = "ducktape/skills/freecad/examples/compound/drawing.dxf"
_GOLDEN_SVG = "ducktape/skills/freecad/examples/compound/drawing.svg"
_GOLDEN_PDF = "ducktape/skills/freecad/examples/compound/drawing.pdf"


@pytest.fixture(scope="module")
def compound_outputs(stage_runner: StageRunner) -> Path:
    """Build compound shape and export all formats."""
    return stage_runner.run(
        "compound", Stage(_BUILD_SCRIPT, "build_compound"), Stage(_EXPORT_SCRIPT, "export_page"), fcstd="compound.FCStd"
    )


def test_dxf_golden(runfiles: Runfiles, compound_outputs: Path) -> None:
    assert_dxf_equal(compound_outputs / "compound.dxf", runfiles.root() / _GOLDEN_DXF)


def test_svg_golden(runfiles: Runfiles, compound_outputs: Path) -> None:
    assert_svg_equal(compound_outputs / "compound.svg", runfiles.root() / _GOLDEN_SVG)


def test_pdf_golden(runfiles: Runfiles, compound_outputs: Path) -> None:
    assert_pdf_equal(compound_outputs / "compound.pdf", runfiles.root() / _GOLDEN_PDF)


if __name__ == "__main__":
    pytest_bazel.main()
