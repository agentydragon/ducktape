"""Golden-file test: debug renderers produce color-coded edge/face PNGs."""

from pathlib import Path

import pytest
import pytest_bazel
from PIL import Image
from python.runfiles import Runfiles

from skills.freecad.conftest import Stage, StageRunner
from skills.freecad.testing.compare import assert_png_equal

# The scripts below are driven by runfiles path; gazelle cannot see the dependency.
# gazelle:include_dep //skills/freecad/examples/bearing_block:build
# gazelle:include_dep //skills/freecad/examples/bearing_block:build_techdraw
# gazelle:include_dep //skills/freecad/examples:render_debug_edges
# gazelle:include_dep //skills/freecad/examples:render_debug_faces

_BUILD_SCRIPT = "ducktape/skills/freecad/examples/bearing_block/build.py"
_TECHDRAW_SCRIPT = "ducktape/skills/freecad/examples/bearing_block/build_techdraw.py"
_DEBUG_EDGES_SCRIPT = "ducktape/skills/freecad/examples/render_debug_edges.py"
_DEBUG_FACES_SCRIPT = "ducktape/skills/freecad/examples/render_debug_faces.py"

_GOLDEN_EDGES_FRONT = "ducktape/skills/freecad/examples/bearing_block/debug_edges.png"
_GOLDEN_FACES = "ducktape/skills/freecad/examples/bearing_block/debug_faces.png"

# Debug renderers use QPainter text rendering which varies more than 3D renders.
_DEBUG_MAX_DIFF = 0.05


@pytest.fixture(scope="module")
def debug_outputs(stage_runner: StageRunner) -> Path:
    """Build bearing block, add TechDraw, run debug renderers."""
    return stage_runner.run(
        "debug-renderers",
        Stage(_BUILD_SCRIPT, "build", gui=False),  # Part Design model, pure freecadcmd
        Stage(_TECHDRAW_SCRIPT, "techdraw"),  # TechDraw views, needed for debug edges
        Stage(_DEBUG_EDGES_SCRIPT, "debug_edges"),  # all views
        Stage(_DEBUG_FACES_SCRIPT, "debug_faces"),
        fcstd="bearing_block.FCStd",
    )


def test_debug_edges_produces_png(runfiles: Runfiles, debug_outputs: Path) -> None:
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
    assert_png_equal(actual, runfiles.root() / _GOLDEN_EDGES_FRONT, max_diff_fraction=_DEBUG_MAX_DIFF)


def test_debug_faces_produces_png(runfiles: Runfiles, debug_outputs: Path) -> None:
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
    assert_png_equal(actual, runfiles.root() / _GOLDEN_FACES, max_diff_fraction=_DEBUG_MAX_DIFF)


if __name__ == "__main__":
    pytest_bazel.main()
