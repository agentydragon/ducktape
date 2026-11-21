"""Golden-file test: parametric_sketch.py -> export_page.py -> DXF/SVG/PDF."""

from pathlib import Path

import pytest
import pytest_bazel
from python.runfiles import Runfiles

from skills.freecad.conftest import Stage, StageRunner
from skills.freecad.testing.compare import assert_dxf_equal, assert_pdf_equal, assert_svg_equal

# The scripts below are driven by runfiles path; gazelle cannot see the dependency.
# gazelle:include_dep //skills/freecad/examples/bracket:parametric_sketch
# gazelle:include_dep //skills/freecad/examples:export_page

_PARAMETRIC_SKETCH = "ducktape/skills/freecad/examples/bracket/parametric_sketch.py"
_EXPORT_PAGE = "ducktape/skills/freecad/examples/export_page.py"
_GOLDEN_DXF = "ducktape/skills/freecad/examples/bracket/drawing.dxf"
_GOLDEN_SVG = "ducktape/skills/freecad/examples/bracket/drawing.svg"
_GOLDEN_PDF = "ducktape/skills/freecad/examples/bracket/drawing.pdf"


@pytest.fixture(scope="module")
def export_outputs(stage_runner: StageRunner) -> Path:
    """Run parametric_sketch.py then export_page.py and return the output directory."""
    return stage_runner.run(
        "parametric-sketch",
        Stage(_PARAMETRIC_SKETCH, "parametric_sketch"),
        Stage(_EXPORT_PAGE, "export_page"),
        fcstd="bracket.FCStd",
    )


def test_dxf_golden(runfiles: Runfiles, export_outputs: Path) -> None:
    assert_dxf_equal(export_outputs / "bracket.dxf", runfiles.root() / _GOLDEN_DXF)


def test_svg_golden(runfiles: Runfiles, export_outputs: Path) -> None:
    assert_svg_equal(export_outputs / "bracket.svg", runfiles.root() / _GOLDEN_SVG)


def test_pdf_golden(runfiles: Runfiles, export_outputs: Path) -> None:
    assert_pdf_equal(export_outputs / "bracket.pdf", runfiles.root() / _GOLDEN_PDF)


if __name__ == "__main__":
    pytest_bazel.main()
