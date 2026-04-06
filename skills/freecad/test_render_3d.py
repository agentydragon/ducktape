"""Golden-file test: 3D cube with hole -> FCStd -> render PNG via AppImage."""

import shutil
from pathlib import Path

import pytest_bazel

from skills.freecad.testing.compare import assert_png_equal
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

_BUILD_SCRIPT = "_main/skills/freecad/build_cube_with_hole.py"
_RENDER_SCRIPT = "_main/skills/freecad/render_fcstd.py"
_GOLDEN = "_main/skills/freecad/golden/cube_with_hole.png"


def test_render_3d(tmp_path: Path, freecad_headless, freecad_gui) -> None:
    build_script = get_required_path(_BUILD_SCRIPT)
    render_script = get_required_path(_RENDER_SCRIPT)
    golden_path = get_required_path(_GOLDEN)

    # Step 1: build FCStd headlessly (no GUI needed for solid geometry)
    result = freecad_headless(build_script, outdir=tmp_path)
    assert result.returncode == 0, (
        f"build failed (exit {result.returncode}):\nstdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}"
    )

    fcstd = tmp_path / "cube_with_hole.FCStd"
    assert fcstd.exists(), f"FCStd not generated\nstderr: {result.stderr[:500]}"

    # Step 2: render PNG via GUI binary (needs OpenGL/Coin for 3D view)
    result2 = freecad_gui(render_script, outdir=tmp_path, env={"INPUT": str(fcstd)})
    assert result2.returncode == 0, (
        f"render failed (exit {result2.returncode}):\nstdout: {result2.stdout[:500]}\nstderr: {result2.stderr[:500]}"
    )

    actual_png = tmp_path / "cube_with_hole.png"
    assert actual_png.exists(), f"PNG not generated\nstderr: {result2.stderr[:500]}"

    out_dir = undeclared_outputs_dir() / "render-3d"
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(actual_png, out_dir / "actual.png")

    assert_png_equal(actual_png, golden_path)


if __name__ == "__main__":
    pytest_bazel.main()
