"""Golden-file test: build_compound.py -> export_page.py -> DXF/SVG/PDF via AppImage."""

import shutil
from pathlib import Path

import pytest
import pytest_bazel

from skills.freecad.testing.compare import assert_dxf_equal, assert_pdf_equal, assert_svg_equal
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

_BUILD_SCRIPT = "_main/skills/freecad/build_compound.py"
_EXPORT_SCRIPT = "_main/skills/freecad/export_page.py"
_GOLDEN_DXF = "_main/skills/freecad/golden/compound.dxf"
_GOLDEN_SVG = "_main/skills/freecad/golden/compound.svg"
_GOLDEN_PDF = "_main/skills/freecad/golden/compound.pdf"


def _save_logs(uo: Path, name: str, result) -> None:
    """Write subprocess stdout/stderr to undeclared outputs for post-mortem debugging."""
    if result.stdout:
        (uo / f"{name}.stdout").write_text(result.stdout)
    if result.stderr:
        (uo / f"{name}.stderr").write_text(result.stderr)


@pytest.fixture(scope="module")
def compound_outputs(tmp_path_factory: pytest.TempPathFactory, freecad_headless) -> Path:
    """Build compound shape and export all formats."""
    out_dir = tmp_path_factory.mktemp("compound")
    uo = undeclared_outputs_dir() / "compound"
    uo.mkdir(parents=True, exist_ok=True)

    build_script = get_required_path(_BUILD_SCRIPT)
    result = freecad_headless(build_script, outdir=out_dir)
    _save_logs(uo, "build_compound", result)
    assert result.returncode == 0, (
        f"build_compound.py failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout[:1000]}\nstderr: {result.stderr[:1000]}"
    )

    fcstd = out_dir / "compound.FCStd"
    assert fcstd.exists(), f"compound.FCStd not produced\nstderr: {result.stderr[:500]}"

    export_script = get_required_path(_EXPORT_SCRIPT)
    result2 = freecad_headless(export_script, outdir=out_dir, env={"INPUT": str(fcstd)})
    _save_logs(uo, "export_page", result2)
    assert result2.returncode == 0, (
        f"export_page.py failed (exit {result2.returncode}):\n"
        f"stdout: {result2.stdout[:1000]}\nstderr: {result2.stderr[:1000]}"
    )

    for ext in ("dxf", "svg", "pdf", "FCStd"):
        src = out_dir / f"compound.{ext}"
        if src.exists():
            shutil.copy2(src, uo / f"compound.{ext}")

    return out_dir


def test_dxf_golden(compound_outputs: Path) -> None:
    assert_dxf_equal(compound_outputs / "compound.dxf", get_required_path(_GOLDEN_DXF))


def test_svg_golden(compound_outputs: Path) -> None:
    assert_svg_equal(compound_outputs / "compound.svg", get_required_path(_GOLDEN_SVG))


def test_pdf_golden(compound_outputs: Path) -> None:
    assert_pdf_equal(compound_outputs / "compound.pdf", get_required_path(_GOLDEN_PDF))


if __name__ == "__main__":
    pytest_bazel.main()
