"""Golden-file test: parametric L-bracket via PartDesign -> FCStd -> render two PNG views."""

import shutil
from pathlib import Path

import pytest_bazel
from PIL import Image

from util.bazel.runfiles import get_required_path
from util.oci import OciImage, load_oci_image
from util.testing.container_logs import LoggedContainer
from util.testing.undeclared_outputs import undeclared_outputs_dir

_IMAGE = OciImage("_main/skills/freecad/freecad_test.rloc", "freecad-test:pinned")
_BUILD_SCRIPT = "_main/skills/freecad/build_bracket.py"
_RENDER_SCRIPT = "_main/skills/freecad/render_bracket.py"
_GOLDEN_FRONT = "_main/skills/freecad/golden/bracket_front.png"
_GOLDEN_REAR = "_main/skills/freecad/golden/bracket_rear.png"

_MAX_DIFF_FRACTION = 0.02


def _compare_images(actual_path: Path, golden_path: Path, label: str) -> None:
    actual = Image.open(actual_path).convert("RGB")
    golden = Image.open(golden_path).convert("RGB")
    assert actual.size == golden.size, f"{label}: size mismatch {actual.size} vs {golden.size}"

    a_data = actual.tobytes()
    g_data = golden.tobytes()
    differing = sum(1 for a, g in zip(a_data, g_data, strict=True) if a != g)
    diff_fraction = differing / len(a_data)
    assert diff_fraction <= _MAX_DIFF_FRACTION, (
        f"{label}: differs by {diff_fraction:.1%} (threshold {_MAX_DIFF_FRACTION:.1%})"
    )


def test_render_bracket(tmp_path: Path) -> None:
    load_oci_image(_IMAGE)
    build_script = get_required_path(_BUILD_SCRIPT)
    render_script = get_required_path(_RENDER_SCRIPT)

    # Step 1: Build the bracket FCStd
    with LoggedContainer(
        _IMAGE.tag,
        test_name="bracket-build",
        command="sleep infinity",
        volumes=[(str(build_script), "/work/build_bracket.py", "ro"), (str(tmp_path), "/output", "rw")],
        docker_client_kw={"timeout": 120},
    ) as container:
        result = container.exec('bash -c "OUTDIR=/output freecadcmd /work/build_bracket.py"')
        assert result.exit_code == 0, (
            f"build failed (exit {result.exit_code}): {result.output.decode(errors='replace')[:2000]}"
        )

    fcstd = tmp_path / "bracket.FCStd"
    assert fcstd.exists(), "FCStd not generated — check container logs"

    # Step 2: Render from two angles
    with LoggedContainer(
        _IMAGE.tag,
        test_name="bracket-render",
        command="sleep infinity",
        volumes=[
            (str(render_script), "/work/render_bracket.py", "ro"),
            (str(fcstd), "/work/bracket.FCStd", "ro"),
            (str(tmp_path), "/output", "rw"),
        ],
        docker_client_kw={"timeout": 120},
    ) as container:
        result = container.exec(
            'bash -c "INPUT=/work/bracket.FCStd OUTDIR=/output '
            'xvfb-run -a -s \\"-screen 0 1024x768x24\\" freecadcmd /work/render_bracket.py"'
        )
        assert result.exit_code == 0, (
            f"render failed (exit {result.exit_code}): {result.output.decode(errors='replace')[:2000]}"
        )

    # Save rendered outputs for debugging
    out_dir = undeclared_outputs_dir() / "render-bracket"
    out_dir.mkdir(parents=True, exist_ok=True)

    for name in ["bracket_front.png", "bracket_rear.png"]:
        actual = tmp_path / name
        assert actual.exists(), f"{name} not generated — check container logs"
        shutil.copy(actual, out_dir / name)

    # Step 3: Compare against goldens
    _compare_images(tmp_path / "bracket_front.png", get_required_path(_GOLDEN_FRONT), "front view")
    _compare_images(tmp_path / "bracket_rear.png", get_required_path(_GOLDEN_REAR), "rear view")


if __name__ == "__main__":
    pytest_bazel.main()
