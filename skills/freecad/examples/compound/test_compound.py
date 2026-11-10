"""Golden-file test: build_compound.py -> export_page.py -> DXF/SVG/PDF via AppImage."""

from pathlib import Path

import pytest
import pytest_bazel
from python.runfiles import Runfiles

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from skills.freecad.conftest import assert_run_ok, copy_outputs
from skills.freecad.testing.compare import assert_dxf_equal, assert_pdf_equal, assert_svg_equal

# The scripts below are driven by runfiles path; gazelle cannot see the dependency.
# gazelle:include_dep //skills/freecad/examples/compound:build
# gazelle:include_dep //skills/freecad/examples:export_page

_BUILD_SCRIPT = "ducktape/skills/freecad/examples/compound/build.py"
_EXPORT_SCRIPT = "ducktape/skills/freecad/examples/export_page.py"
_GOLDEN_DXF = "ducktape/skills/freecad/examples/compound/drawing.dxf"
_GOLDEN_SVG = "ducktape/skills/freecad/examples/compound/drawing.svg"
_GOLDEN_PDF = "ducktape/skills/freecad/examples/compound/drawing.pdf"


@pytest.fixture(scope="module")
def compound_outputs(runfiles: Runfiles, tmp_path_factory: pytest.TempPathFactory, freecad_gui) -> Path:
    """Build compound shape and export all formats."""
    out_dir = tmp_path_factory.mktemp("compound")
    uo = undeclared_outputs_dir() / "compound"
    uo.mkdir(parents=True, exist_ok=True)

    runfiles_root = runfiles.root()
    result = freecad_gui(runfiles_root / _BUILD_SCRIPT, outdir=out_dir)
    assert_run_ok(result, "build_compound.py", uo, "build_compound")

    fcstd = out_dir / "compound.FCStd"
    assert fcstd.exists(), "compound.FCStd not produced — see build_compound.stderr in test outputs"

    result2 = freecad_gui(runfiles_root / _EXPORT_SCRIPT, outdir=out_dir, env={"INPUT": str(fcstd)})
    assert_run_ok(result2, "export_page.py", uo, "export_page")

    copy_outputs(out_dir, uo)

    return out_dir


def test_dxf_golden(runfiles: Runfiles, compound_outputs: Path) -> None:
    assert_dxf_equal(compound_outputs / "compound.dxf", runfiles.root() / _GOLDEN_DXF)


def test_svg_golden(runfiles: Runfiles, compound_outputs: Path) -> None:
    assert_svg_equal(compound_outputs / "compound.svg", runfiles.root() / _GOLDEN_SVG)


def test_pdf_golden(runfiles: Runfiles, compound_outputs: Path) -> None:
    assert_pdf_equal(compound_outputs / "compound.pdf", runfiles.root() / _GOLDEN_PDF)


if __name__ == "__main__":
    pytest_bazel.main()
