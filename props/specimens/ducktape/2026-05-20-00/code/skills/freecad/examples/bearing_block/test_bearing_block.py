"""Golden-file test: Part Design bearing block -> TechDraw + 3D renders."""

from pathlib import Path

import pytest
import pytest_bazel

from skills.freecad.conftest import assert_run_ok, copy_outputs
from skills.freecad.testing.compare import assert_dxf_equal, assert_pdf_equal, assert_png_equal, assert_svg_equal
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

_BUILD_SCRIPT = "_main/skills/freecad/examples/bearing_block/build.py"
_TECHDRAW_SCRIPT = "_main/skills/freecad/examples/bearing_block/build_techdraw.py"
_RENDER_SCRIPT = "_main/skills/freecad/examples/render_multi_angle.py"
_EXPORT_SCRIPT = "_main/skills/freecad/examples/export_page.py"
_VERIFY_SCRIPT = "_main/skills/freecad/examples/verify_fcstd_load.py"

_GOLDEN_DXF = "_main/skills/freecad/examples/bearing_block/drawing.dxf"
_GOLDEN_SVG = "_main/skills/freecad/examples/bearing_block/drawing.svg"
_GOLDEN_PDF = "_main/skills/freecad/examples/bearing_block/drawing.pdf"
_GOLDEN_FRONT_RIGHT = "_main/skills/freecad/examples/bearing_block/front_right.png"
_GOLDEN_BACK_LEFT = "_main/skills/freecad/examples/bearing_block/back_left.png"


@pytest.fixture(scope="module")
def bearing_block_outputs(tmp_path_factory: pytest.TempPathFactory, freecad_run, freecad_gui) -> Path:
    """Build bearing block, export TechDraw, render perspectives."""
    out_dir = tmp_path_factory.mktemp("bearing-block")
    uo = undeclared_outputs_dir() / "bearing-block"
    uo.mkdir(parents=True, exist_ok=True)

    # Stage 1: Build the Part Design model (pure freecadcmd, no GUI)
    result = freecad_run(get_required_path(_BUILD_SCRIPT), outdir=out_dir)
    assert_run_ok(result, "build_bearing_block.py", uo, "build")

    fcstd = out_dir / "bearing_block.FCStd"
    assert fcstd.exists(), "FCStd not generated — see build.stderr in test outputs"

    # Stage 2: Add TechDraw views + dimensions
    result = freecad_gui(get_required_path(_TECHDRAW_SCRIPT), outdir=out_dir, env={"INPUT": str(fcstd)})
    assert_run_ok(result, "build_bearing_block_techdraw.py", uo, "techdraw")

    # Stage 3: Export TechDraw to DXF/SVG/PDF
    result = freecad_gui(get_required_path(_EXPORT_SCRIPT), outdir=out_dir, env={"INPUT": str(fcstd)})
    assert_run_ok(result, "export_page.py", uo, "export")

    # Stage 4: Render multiple 3D perspectives
    result = freecad_gui(get_required_path(_RENDER_SCRIPT), outdir=out_dir, env={"INPUT": str(fcstd)})
    assert_run_ok(result, "render_multi_angle.py", uo, "render")

    copy_outputs(out_dir, uo)

    return out_dir


def test_techdraw_dxf_golden(bearing_block_outputs: Path) -> None:
    assert_dxf_equal(bearing_block_outputs / "bearing_block.dxf", get_required_path(_GOLDEN_DXF))


def test_techdraw_svg_golden(bearing_block_outputs: Path) -> None:
    assert_svg_equal(bearing_block_outputs / "bearing_block.svg", get_required_path(_GOLDEN_SVG))


def test_techdraw_pdf_golden(bearing_block_outputs: Path) -> None:
    assert_pdf_equal(bearing_block_outputs / "bearing_block.pdf", get_required_path(_GOLDEN_PDF))


def test_render_front_right(bearing_block_outputs: Path) -> None:
    assert_png_equal(bearing_block_outputs / "bearing_block_front_right.png", get_required_path(_GOLDEN_FRONT_RIGHT))


def test_render_back_left(bearing_block_outputs: Path) -> None:
    assert_png_equal(bearing_block_outputs / "bearing_block_back_left.png", get_required_path(_GOLDEN_BACK_LEFT))


def test_fcstd_round_trip(bearing_block_outputs: Path, freecad_run) -> None:
    """Verify the FCStd reloads cleanly in a fresh FreeCAD instance."""
    fcstd = bearing_block_outputs / "bearing_block.FCStd"
    assert fcstd.exists()

    uo = undeclared_outputs_dir() / "bearing-block"
    uo.mkdir(parents=True, exist_ok=True)
    reload_dir = bearing_block_outputs / "reload"
    reload_dir.mkdir(exist_ok=True)

    result = freecad_run(get_required_path(_VERIFY_SCRIPT), outdir=reload_dir, env={"INPUT": str(fcstd)})
    assert_run_ok(result, "verify_fcstd_load.py", uo, "reload")


if __name__ == "__main__":
    pytest_bazel.main()
