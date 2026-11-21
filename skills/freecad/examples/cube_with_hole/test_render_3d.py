"""Golden-file test: cube with hole -> FCStd -> render PNG."""

import shutil
from pathlib import Path

import pytest
import pytest_bazel
from python.runfiles import Runfiles

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from skills.freecad.conftest import Stage, StageRunner
from skills.freecad.testing.compare import assert_png_equal

# The scripts below are driven by runfiles path; gazelle cannot see the dependency.
# gazelle:include_dep //skills/freecad/examples/cube_with_hole:build
# gazelle:include_dep //skills/freecad/examples:render_fcstd

_BUILD_SCRIPT = "ducktape/skills/freecad/examples/cube_with_hole/build.py"
_RENDER_SCRIPT = "ducktape/skills/freecad/examples/render_fcstd.py"
_GOLDEN = "ducktape/skills/freecad/examples/cube_with_hole/render.png"


@pytest.fixture(scope="module")
def render_outputs(runfiles: Runfiles, stage_runner: StageRunner) -> Path:
    """Build FCStd with freecadcmd, render PNG with freecad GUI binary."""
    name = "render-3d"
    out_dir = stage_runner.run(
        name, Stage(_BUILD_SCRIPT, "build", gui=False), Stage(_RENDER_SCRIPT, "render"), fcstd="cube_with_hole.FCStd"
    )
    assert (out_dir / "cube_with_hole.png").exists(), "PNG not generated — see render.stderr in test outputs"
    shutil.copy2(runfiles.root() / _GOLDEN, undeclared_outputs_dir() / name / "golden.png")
    return out_dir


def test_render_3d_golden(runfiles: Runfiles, render_outputs: Path) -> None:
    assert_png_equal(render_outputs / "cube_with_hole.png", runfiles.root() / _GOLDEN)


if __name__ == "__main__":
    pytest_bazel.main()
