"""Golden-file test: Part Design bearing block -> TechDraw + 3D renders."""

from pathlib import Path

import pytest
import pytest_bazel
from python.runfiles import Runfiles

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from skills.freecad.conftest import Stage, StageRunner, assert_run_ok
from skills.freecad.testing.compare import assert_dxf_equal, assert_pdf_equal, assert_png_equal, assert_svg_equal

# The scripts below are driven by runfiles path; gazelle cannot see the dependency.
# gazelle:include_dep //skills/freecad/examples/bearing_block:build
# gazelle:include_dep //skills/freecad/examples/bearing_block:build_techdraw
# gazelle:include_dep //skills/freecad/examples:render_multi_angle
# gazelle:include_dep //skills/freecad/examples:export_page
# gazelle:include_dep //skills/freecad/examples:verify_fcstd_load

_BUILD_SCRIPT = "ducktape/skills/freecad/examples/bearing_block/build.py"
_TECHDRAW_SCRIPT = "ducktape/skills/freecad/examples/bearing_block/build_techdraw.py"
_RENDER_SCRIPT = "ducktape/skills/freecad/examples/render_multi_angle.py"
_EXPORT_SCRIPT = "ducktape/skills/freecad/examples/export_page.py"
_VERIFY_SCRIPT = "ducktape/skills/freecad/examples/verify_fcstd_load.py"

_GOLDEN_DXF = "ducktape/skills/freecad/examples/bearing_block/drawing.dxf"
_GOLDEN_SVG = "ducktape/skills/freecad/examples/bearing_block/drawing.svg"
_GOLDEN_PDF = "ducktape/skills/freecad/examples/bearing_block/drawing.pdf"
_GOLDEN_FRONT_RIGHT = "ducktape/skills/freecad/examples/bearing_block/front_right.png"
_GOLDEN_BACK_LEFT = "ducktape/skills/freecad/examples/bearing_block/back_left.png"


@pytest.fixture(scope="module")
def bearing_block_outputs(stage_runner: StageRunner) -> Path:
    """Build bearing block, export TechDraw, render perspectives."""
    return stage_runner.run(
        "bearing-block",
        Stage(_BUILD_SCRIPT, "build", gui=False),  # Part Design model, pure freecadcmd
        Stage(_TECHDRAW_SCRIPT, "techdraw"),  # TechDraw views + dimensions
        Stage(_EXPORT_SCRIPT, "export"),  # TechDraw to DXF/SVG/PDF
        Stage(_RENDER_SCRIPT, "render"),  # multiple 3D perspectives
        fcstd="bearing_block.FCStd",
    )


def test_techdraw_dxf_golden(runfiles: Runfiles, bearing_block_outputs: Path) -> None:
    assert_dxf_equal(bearing_block_outputs / "bearing_block.dxf", runfiles.root() / _GOLDEN_DXF)


def test_techdraw_svg_golden(runfiles: Runfiles, bearing_block_outputs: Path) -> None:
    assert_svg_equal(bearing_block_outputs / "bearing_block.svg", runfiles.root() / _GOLDEN_SVG)


def test_techdraw_pdf_golden(runfiles: Runfiles, bearing_block_outputs: Path) -> None:
    assert_pdf_equal(bearing_block_outputs / "bearing_block.pdf", runfiles.root() / _GOLDEN_PDF)


def test_render_front_right(runfiles: Runfiles, bearing_block_outputs: Path) -> None:
    assert_png_equal(bearing_block_outputs / "bearing_block_front_right.png", runfiles.root() / _GOLDEN_FRONT_RIGHT)


def test_render_back_left(runfiles: Runfiles, bearing_block_outputs: Path) -> None:
    assert_png_equal(bearing_block_outputs / "bearing_block_back_left.png", runfiles.root() / _GOLDEN_BACK_LEFT)


def test_fcstd_round_trip(runfiles: Runfiles, bearing_block_outputs: Path, freecad_run) -> None:
    """Verify the FCStd reloads cleanly in a fresh FreeCAD instance."""
    fcstd = bearing_block_outputs / "bearing_block.FCStd"
    assert fcstd.exists()

    uo = undeclared_outputs_dir() / "bearing-block"
    uo.mkdir(parents=True, exist_ok=True)
    reload_dir = bearing_block_outputs / "reload"
    reload_dir.mkdir(exist_ok=True)

    result = freecad_run(runfiles.root() / _VERIFY_SCRIPT, outdir=reload_dir, env={"INPUT": str(fcstd)})
    assert_run_ok(result, "verify_fcstd_load.py", uo, "reload")


if __name__ == "__main__":
    pytest_bazel.main()
