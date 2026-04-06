"""Golden-file test: parametric_sketch.py -> export_page.py -> DXF/SVG/PDF via AppImage."""

import shutil
from pathlib import Path

import pytest
import pytest_bazel

from skills.freecad.testing.compare import assert_dxf_equal, assert_pdf_equal, assert_svg_equal
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

_PARAMETRIC_SKETCH = "_main/skills/freecad/parametric_sketch.py"
_EXPORT_PAGE = "_main/skills/freecad/export_page.py"
_GOLDEN_DXF = "_main/skills/freecad/golden/bracket.dxf"
_GOLDEN_SVG = "_main/skills/freecad/golden/bracket.svg"
_GOLDEN_PDF = "_main/skills/freecad/golden/bracket.pdf"


def _save_logs(uo: Path, name: str, result) -> None:
    """Write subprocess stdout/stderr to undeclared outputs for post-mortem debugging."""
    if result.stdout:
        (uo / f"{name}.stdout").write_text(result.stdout)
    if result.stderr:
        (uo / f"{name}.stderr").write_text(result.stderr)


@pytest.fixture(scope="module")
def export_outputs(tmp_path_factory: pytest.TempPathFactory, freecad_headless) -> Path:
    """Run parametric_sketch.py then export_page.py and return the output directory."""
    out_dir = tmp_path_factory.mktemp("parametric-sketch")
    uo = undeclared_outputs_dir() / "parametric-sketch"
    uo.mkdir(parents=True, exist_ok=True)

    script = get_required_path(_PARAMETRIC_SKETCH)
    result = freecad_headless(script, outdir=out_dir)
    _save_logs(uo, "parametric_sketch", result)
    assert result.returncode == 0, (
        f"parametric_sketch.py failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout[:1000]}\nstderr: {result.stderr[:1000]}"
    )

    fcstd = out_dir / "bracket.FCStd"
    assert fcstd.exists(), f"bracket.FCStd not produced\nstderr: {result.stderr[:500]}"

    export_script = get_required_path(_EXPORT_PAGE)
    result2 = freecad_headless(export_script, outdir=out_dir, env={"INPUT": str(fcstd)})
    _save_logs(uo, "export_page", result2)
    assert result2.returncode == 0, (
        f"export_page.py failed (exit {result2.returncode}):\n"
        f"stdout: {result2.stdout[:1000]}\nstderr: {result2.stderr[:1000]}"
    )

    for ext in ("dxf", "svg", "pdf", "FCStd", "log"):
        src = out_dir / f"bracket.{ext}" if ext != "log" else out_dir / "freecad.log"
        if src.exists():
            shutil.copy2(src, uo / src.name)

    return out_dir


def test_dxf_golden(export_outputs: Path) -> None:
    assert_dxf_equal(export_outputs / "bracket.dxf", get_required_path(_GOLDEN_DXF))


def test_svg_golden(export_outputs: Path) -> None:
    assert_svg_equal(export_outputs / "bracket.svg", get_required_path(_GOLDEN_SVG))


def test_pdf_golden(export_outputs: Path) -> None:
    assert_pdf_equal(export_outputs / "bracket.pdf", get_required_path(_GOLDEN_PDF))


if __name__ == "__main__":
    pytest_bazel.main()
