"""Golden-file test: debug renderers produce color-coded edge/face PNGs."""

import shutil
from pathlib import Path

import pytest
import pytest_bazel
from PIL import Image

from skills.freecad.testing.compare import assert_png_equal
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

_BUILD_SCRIPT = "_main/skills/freecad/build_bearing_block.py"
_TECHDRAW_SCRIPT = "_main/skills/freecad/build_bearing_block_techdraw.py"
_DEBUG_EDGES_SCRIPT = "_main/skills/freecad/render_debug_edges.py"
_DEBUG_FACES_SCRIPT = "_main/skills/freecad/render_debug_faces.py"

_GOLDEN_EDGES_FRONT = "_main/skills/freecad/golden/FrontView_debug_edges.png"
_GOLDEN_FACES = "_main/skills/freecad/golden/debug_faces.png"

# Debug renderers use QPainter text rendering which varies more than 3D renders.
_DEBUG_MAX_DIFF = 0.05


def _save_logs(uo: Path, name: str, result) -> None:
    if result.stdout:
        (uo / f"{name}.stdout").write_text(result.stdout)
    if result.stderr:
        (uo / f"{name}.stderr").write_text(result.stderr)


@pytest.fixture(scope="module")
def debug_outputs(tmp_path_factory: pytest.TempPathFactory, freecad_headless) -> Path:
    """Build bearing block, add TechDraw, run debug renderers."""
    out_dir = tmp_path_factory.mktemp("debug-renderers")
    uo = undeclared_outputs_dir() / "debug-renderers"
    uo.mkdir(parents=True, exist_ok=True)

    # Build the Part Design model
    result = freecad_headless(get_required_path(_BUILD_SCRIPT), outdir=out_dir)
    _save_logs(uo, "build", result)
    assert result.returncode == 0, (
        f"build_bearing_block.py failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout[:1000]}\nstderr: {result.stderr[:1000]}"
    )

    fcstd = out_dir / "bearing_block.FCStd"
    assert fcstd.exists(), f"FCStd not generated\nstderr: {result.stderr[:500]}"

    # Add TechDraw views (needed for debug edges)
    result = freecad_headless(get_required_path(_TECHDRAW_SCRIPT), outdir=out_dir, env={"INPUT": str(fcstd)})
    _save_logs(uo, "techdraw", result)
    assert result.returncode == 0, (
        f"build_bearing_block_techdraw.py failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout[:1000]}\nstderr: {result.stderr[:1000]}"
    )

    # Render debug edges (all views)
    result = freecad_headless(get_required_path(_DEBUG_EDGES_SCRIPT), outdir=out_dir, env={"INPUT": str(fcstd)})
    _save_logs(uo, "debug_edges", result)
    assert result.returncode == 0, (
        f"render_debug_edges.py failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout[:1000]}\nstderr: {result.stderr[:1000]}"
    )

    # Render debug faces
    result = freecad_headless(get_required_path(_DEBUG_FACES_SCRIPT), outdir=out_dir, env={"INPUT": str(fcstd)})
    _save_logs(uo, "debug_faces", result)
    assert result.returncode == 0, (
        f"render_debug_faces.py failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout[:1000]}\nstderr: {result.stderr[:1000]}"
    )

    for f in out_dir.iterdir():
        shutil.copy2(f, uo / f.name)

    return out_dir


def test_debug_edges_produces_png(debug_outputs: Path) -> None:
    """Debug edge renderer produces a non-trivial PNG with colored edges."""
    actual = debug_outputs / "FrontView_debug_edges.png"
    assert actual.exists(), "FrontView debug edges PNG not generated"
    img = Image.open(actual)
    assert img.size == (1800, 1350), f"Unexpected size: {img.size}"
    # Verify it's not blank (has non-white pixels = colored edges)
    pixels = img.convert("RGB").tobytes()
    non_white = sum(
        1 for i in range(0, len(pixels), 3) if pixels[i] != 255 or pixels[i + 1] != 255 or pixels[i + 2] != 255
    )
    assert non_white > 100, "Debug edges PNG appears blank (no colored edges)"
    assert_png_equal(actual, get_required_path(_GOLDEN_EDGES_FRONT), max_diff_fraction=_DEBUG_MAX_DIFF)


def test_debug_faces_produces_png(debug_outputs: Path) -> None:
    """Debug face renderer produces a non-trivial PNG with colored faces."""
    actual = debug_outputs / "debug_faces.png"
    assert actual.exists(), "Debug faces PNG not generated"
    img = Image.open(actual)
    assert img.size == (800, 600), f"Unexpected size: {img.size}"
    # Verify it has multiple distinct colors (not monochrome)
    colors = img.convert("RGB").getcolors(maxcolors=10000)
    assert colors is not None, "Could not count colors"
    # A properly colored face render should have many colors (>50 distinct RGB values)
    assert len(colors) > 50, f"Only {len(colors)} distinct colors — faces may not be colored"
    assert_png_equal(actual, get_required_path(_GOLDEN_FACES), max_diff_fraction=_DEBUG_MAX_DIFF)


if __name__ == "__main__":
    pytest_bazel.main()
