"""Golden-file test: debug renderers produce color-coded edge/face PNGs."""

from pathlib import Path

import pytest
import pytest_bazel
from PIL import Image

from skills.freecad.conftest import assert_run_ok, copy_outputs
from skills.freecad.testing.compare import assert_png_equal
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

_BUILD_SCRIPT = "_main/skills/freecad/examples/bearing_block/build.py"
_TECHDRAW_SCRIPT = "_main/skills/freecad/examples/bearing_block/build_techdraw.py"
_DEBUG_EDGES_SCRIPT = "_main/skills/freecad/examples/render_debug_edges.py"
_DEBUG_FACES_SCRIPT = "_main/skills/freecad/examples/render_debug_faces.py"

_GOLDEN_EDGES_FRONT = "_main/skills/freecad/examples/bearing_block/debug_edges.png"
_GOLDEN_FACES = "_main/skills/freecad/examples/bearing_block/debug_faces.png"

# Debug renderers use QPainter text rendering which varies more than 3D renders.
_DEBUG_MAX_DIFF = 0.05


@pytest.fixture(scope="module")
def debug_outputs(tmp_path_factory: pytest.TempPathFactory, freecad_run, freecad_gui) -> Path:
    """Build bearing block, add TechDraw, run debug renderers."""
    out_dir = tmp_path_factory.mktemp("debug-renderers")
    uo = undeclared_outputs_dir() / "debug-renderers"
    uo.mkdir(parents=True, exist_ok=True)

    # Build the Part Design model (pure freecadcmd, no GUI)
    result = freecad_run(get_required_path(_BUILD_SCRIPT), outdir=out_dir)
    assert_run_ok(result, "build_bearing_block.py", uo, "build")

    fcstd = out_dir / "bearing_block.FCStd"
    assert fcstd.exists(), "FCStd not generated — see build.stderr in test outputs"

    # Add TechDraw views (needed for debug edges)
    result = freecad_gui(get_required_path(_TECHDRAW_SCRIPT), outdir=out_dir, env={"INPUT": str(fcstd)})
    assert_run_ok(result, "build_bearing_block_techdraw.py", uo, "techdraw")

    # Render debug edges (all views)
    result = freecad_gui(get_required_path(_DEBUG_EDGES_SCRIPT), outdir=out_dir, env={"INPUT": str(fcstd)})
    assert_run_ok(result, "render_debug_edges.py", uo, "debug_edges")

    # Render debug faces
    result = freecad_gui(get_required_path(_DEBUG_FACES_SCRIPT), outdir=out_dir, env={"INPUT": str(fcstd)})
    assert_run_ok(result, "render_debug_faces.py", uo, "debug_faces")

    copy_outputs(out_dir, uo)

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
