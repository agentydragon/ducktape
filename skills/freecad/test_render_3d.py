"""Golden-file test: cube with hole -> FCStd -> render PNG via FreeCAD AppImage."""

import shutil
from pathlib import Path

import pytest
import pytest_bazel

from skills.freecad.testing.compare import assert_png_equal
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

_BUILD_SCRIPT = "_main/skills/freecad/build_cube_with_hole.py"
_RENDER_SCRIPT = "_main/skills/freecad/render_fcstd.py"
_GOLDEN = "_main/skills/freecad/golden/cube_with_hole.png"


def _save_logs(uo: Path, name: str, result) -> None:
    if result.stdout:
        (uo / f"{name}.stdout").write_text(result.stdout)
    if result.stderr:
        (uo / f"{name}.stderr").write_text(result.stderr)


@pytest.fixture(scope="module")
def render_outputs(tmp_path_factory: pytest.TempPathFactory, freecad_headless, freecad_gui) -> Path:
    """Build FCStd with freecadcmd, render PNG with freecad GUI binary."""
    out_dir = tmp_path_factory.mktemp("render-3d")
    uo = undeclared_outputs_dir() / "render-3d"
    uo.mkdir(parents=True, exist_ok=True)

    build_script = get_required_path(_BUILD_SCRIPT)
    result = freecad_headless(build_script, outdir=out_dir)
    _save_logs(uo, "build", result)
    assert result.returncode == 0, (
        f"build_cube_with_hole.py failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout[:1000]}\nstderr: {result.stderr[:1000]}"
    )

    fcstd = out_dir / "cube_with_hole.FCStd"
    assert fcstd.exists(), f"FCStd not generated\nstderr: {result.stderr[:500]}"

    render_script = get_required_path(_RENDER_SCRIPT)
    result2 = freecad_gui(render_script, outdir=out_dir, env={"INPUT": str(fcstd)})
    _save_logs(uo, "render", result2)
    assert result2.returncode == 0, (
        f"render_fcstd.py failed (exit {result2.returncode}):\n"
        f"stdout: {result2.stdout[:1000]}\nstderr: {result2.stderr[:1000]}"
    )

    png = out_dir / "cube_with_hole.png"
    assert png.exists(), f"PNG not generated\nstderr: {result2.stderr[:500]}"
    shutil.copy2(png, uo / "actual.png")
    shutil.copy2(get_required_path(_GOLDEN), uo / "golden.png")
    return out_dir


def test_render_3d_golden(render_outputs: Path) -> None:
    assert_png_equal(render_outputs / "cube_with_hole.png", get_required_path(_GOLDEN))


if __name__ == "__main__":
    pytest_bazel.main()
