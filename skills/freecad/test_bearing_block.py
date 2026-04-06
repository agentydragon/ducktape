"""Golden-file test: Part Design bearing block -> TechDraw + 3D renders."""

import shutil
from pathlib import Path

import pytest
import pytest_bazel

from skills.freecad.testing.compare import assert_dxf_equal, assert_pdf_equal, assert_png_equal, assert_svg_equal
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

_BUILD_SCRIPT = "_main/skills/freecad/build_bearing_block.py"
_TECHDRAW_SCRIPT = "_main/skills/freecad/build_bearing_block_techdraw.py"
_RENDER_SCRIPT = "_main/skills/freecad/render_multi_angle.py"
_EXPORT_SCRIPT = "_main/skills/freecad/export_page.py"

_GOLDEN_DXF = "_main/skills/freecad/golden/bearing_block.dxf"
_GOLDEN_SVG = "_main/skills/freecad/golden/bearing_block.svg"
_GOLDEN_PDF = "_main/skills/freecad/golden/bearing_block.pdf"
_GOLDEN_FRONT_RIGHT = "_main/skills/freecad/golden/bearing_block_front_right.png"
_GOLDEN_BACK_LEFT = "_main/skills/freecad/golden/bearing_block_back_left.png"


def _save_logs(uo: Path, name: str, result) -> None:
    if result.stdout:
        (uo / f"{name}.stdout").write_text(result.stdout)
    if result.stderr:
        (uo / f"{name}.stderr").write_text(result.stderr)


@pytest.fixture(scope="module")
def bearing_block_outputs(tmp_path_factory: pytest.TempPathFactory, freecad_headless) -> Path:
    """Build bearing block, export TechDraw, render perspectives."""
    out_dir = tmp_path_factory.mktemp("bearing-block")
    uo = undeclared_outputs_dir() / "bearing-block"
    uo.mkdir(parents=True, exist_ok=True)

    # Stage 1: Build the Part Design model (pure freecadcmd, no GUI)
    result = freecad_headless(get_required_path(_BUILD_SCRIPT), outdir=out_dir)
    _save_logs(uo, "build", result)
    assert result.returncode == 0, (
        f"build_bearing_block.py failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout[:1000]}\nstderr: {result.stderr[:1000]}"
    )

    fcstd = out_dir / "bearing_block.FCStd"
    assert fcstd.exists(), f"FCStd not generated\nstderr: {result.stderr[:500]}"

    # Stage 2: Add TechDraw views + dimensions
    result = freecad_headless(get_required_path(_TECHDRAW_SCRIPT), outdir=out_dir, env={"INPUT": str(fcstd)})
    _save_logs(uo, "techdraw", result)
    assert result.returncode == 0, (
        f"build_bearing_block_techdraw.py failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout[:1000]}\nstderr: {result.stderr[:1000]}"
    )

    # Stage 3: Export TechDraw to DXF/SVG/PDF
    result = freecad_headless(get_required_path(_EXPORT_SCRIPT), outdir=out_dir, env={"INPUT": str(fcstd)})
    _save_logs(uo, "export", result)
    assert result.returncode == 0, (
        f"export_page.py failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout[:1000]}\nstderr: {result.stderr[:1000]}"
    )

    # Stage 4: Render multiple 3D perspectives
    result = freecad_headless(get_required_path(_RENDER_SCRIPT), outdir=out_dir, env={"INPUT": str(fcstd)})
    _save_logs(uo, "render", result)
    assert result.returncode == 0, (
        f"render_multi_angle.py failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout[:1000]}\nstderr: {result.stderr[:1000]}"
    )

    for f in out_dir.iterdir():
        shutil.copy2(f, uo / f.name)

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


if __name__ == "__main__":
    pytest_bazel.main()
