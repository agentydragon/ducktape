"""Golden-file test: parametric_sketch.py -> export_page.py -> DXF/SVG/PDF via AppImage."""

from pathlib import Path

import pytest
import pytest_bazel

from skills.freecad.conftest import assert_run_ok, copy_outputs
from skills.freecad.testing.compare import assert_dxf_equal, assert_pdf_equal, assert_svg_equal
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

_PARAMETRIC_SKETCH = "_main/skills/freecad/examples/bracket/parametric_sketch.py"
_EXPORT_PAGE = "_main/skills/freecad/examples/export_page.py"
_GOLDEN_DXF = "_main/skills/freecad/examples/bracket/drawing.dxf"
_GOLDEN_SVG = "_main/skills/freecad/examples/bracket/drawing.svg"
_GOLDEN_PDF = "_main/skills/freecad/examples/bracket/drawing.pdf"


@pytest.fixture(scope="module")
def export_outputs(tmp_path_factory: pytest.TempPathFactory, freecad_gui) -> Path:
    """Run parametric_sketch.py then export_page.py and return the output directory."""
    out_dir = tmp_path_factory.mktemp("parametric-sketch")
    uo = undeclared_outputs_dir() / "parametric-sketch"
    uo.mkdir(parents=True, exist_ok=True)

    result = freecad_gui(get_required_path(_PARAMETRIC_SKETCH), outdir=out_dir)
    assert_run_ok(result, "parametric_sketch.py", uo, "parametric_sketch")

    fcstd = out_dir / "bracket.FCStd"
    assert fcstd.exists(), "bracket.FCStd not produced — see parametric_sketch.stderr in test outputs"

    result2 = freecad_gui(get_required_path(_EXPORT_PAGE), outdir=out_dir, env={"INPUT": str(fcstd)})
    assert_run_ok(result2, "export_page.py", uo, "export_page")

    copy_outputs(out_dir, uo)

    return out_dir


def test_dxf_golden(export_outputs: Path) -> None:
    assert_dxf_equal(export_outputs / "bracket.dxf", get_required_path(_GOLDEN_DXF))


def test_svg_golden(export_outputs: Path) -> None:
    assert_svg_equal(export_outputs / "bracket.svg", get_required_path(_GOLDEN_SVG))


def test_pdf_golden(export_outputs: Path) -> None:
    assert_pdf_equal(export_outputs / "bracket.pdf", get_required_path(_GOLDEN_PDF))


if __name__ == "__main__":
    pytest_bazel.main()
