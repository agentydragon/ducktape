"""Golden-file test: cube with hole -> FCStd -> render PNG via FreeCAD AppImage."""

import shutil
from pathlib import Path

import pytest
import pytest_bazel
from python.runfiles import Runfiles

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from skills.freecad.conftest import assert_run_ok, copy_outputs
from skills.freecad.testing.compare import assert_png_equal

# The scripts below are driven by runfiles path; gazelle cannot see the dependency.
# gazelle:include_dep //skills/freecad/examples/cube_with_hole:build
# gazelle:include_dep //skills/freecad/examples:render_fcstd

_BUILD_SCRIPT = "ducktape/skills/freecad/examples/cube_with_hole/build.py"
_RENDER_SCRIPT = "ducktape/skills/freecad/examples/render_fcstd.py"
_GOLDEN = "ducktape/skills/freecad/examples/cube_with_hole/render.png"


@pytest.fixture(scope="module")
def render_outputs(runfiles: Runfiles, tmp_path_factory: pytest.TempPathFactory, freecad_run, freecad_gui) -> Path:
    """Build FCStd with freecadcmd, render PNG with freecad GUI binary."""
    out_dir = tmp_path_factory.mktemp("render-3d")
    uo = undeclared_outputs_dir() / "render-3d"
    uo.mkdir(parents=True, exist_ok=True)

    runfiles_root = runfiles.root()
    result = freecad_run(runfiles_root / _BUILD_SCRIPT, outdir=out_dir)
    assert_run_ok(result, "build_cube_with_hole.py", uo, "build")

    fcstd = out_dir / "cube_with_hole.FCStd"
    assert fcstd.exists(), "FCStd not generated — see build.stderr in test outputs"

    result2 = freecad_gui(runfiles_root / _RENDER_SCRIPT, outdir=out_dir, env={"INPUT": str(fcstd)})
    assert_run_ok(result2, "render_fcstd.py", uo, "render")

    assert (out_dir / "cube_with_hole.png").exists(), "PNG not generated — see render.stderr in test outputs"
    copy_outputs(out_dir, uo)
    shutil.copy2(runfiles_root / _GOLDEN, uo / "golden.png")
    return out_dir


def test_render_3d_golden(runfiles: Runfiles, render_outputs: Path) -> None:
    assert_png_equal(render_outputs / "cube_with_hole.png", runfiles.root() / _GOLDEN)


if __name__ == "__main__":
    pytest_bazel.main()
