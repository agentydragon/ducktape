"""Golden-file test: parametric_sketch.py -> export_page.py -> DXF/SVG/PDF."""

from pathlib import Path

import pytest
import pytest_bazel

from skills.freecad.conftest import FREECAD_TEST
from skills.freecad.testing.compare import assert_dxf_equal, assert_pdf_equal, assert_svg_equal
from util.bazel.runfiles import get_required_path
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer
from util.testing.undeclared_outputs import undeclared_outputs_dir

_PARAMETRIC_SKETCH = "_main/skills/freecad/parametric_sketch.py"
_EXPORT_PAGE = "_main/skills/freecad/export_page.py"
_GOLDEN_DXF = "_main/skills/freecad/golden/bracket.dxf"
_GOLDEN_SVG = "_main/skills/freecad/golden/bracket.svg"
_GOLDEN_PDF = "_main/skills/freecad/golden/bracket.pdf"

_XVFB = 'xvfb-run -a -s \\"-screen 0 1024x768x24\\"'


def _exec(container: LoggedContainer, cmd: str) -> None:
    result = container.exec(cmd)
    output = result.output.decode(errors="replace")
    print(output)
    assert result.exit_code == 0, f"Command failed (exit {result.exit_code}): {output[:500]}"


@pytest.fixture(scope="module")
def export_outputs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run parametric_sketch.py then export_page.py and return the output directory."""
    load_oci_image(FREECAD_TEST)
    out_dir = undeclared_outputs_dir() / "parametric-sketch"
    out_dir.mkdir(parents=True, exist_ok=True)

    with LoggedContainer(
        FREECAD_TEST.tag,
        test_name="freecad-parametric-sketch",
        command="sleep infinity",
        volumes=[
            (str(get_required_path(_PARAMETRIC_SKETCH)), "/work/parametric_sketch.py", "ro"),
            (str(get_required_path(_EXPORT_PAGE)), "/work/export_page.py", "ro"),
            (str(out_dir), "/output", "rw"),
        ],
        docker_client_kw={"timeout": 120},
    ) as container:
        _exec(container, f'bash -c "OUTDIR=/output {_XVFB} freecadcmd /work/parametric_sketch.py"')
        _exec(
            container, f'bash -c "INPUT=/output/bracket.FCStd OUTDIR=/output {_XVFB} freecadcmd /work/export_page.py"'
        )

    return out_dir


def test_dxf_golden(export_outputs: Path) -> None:
    actual = export_outputs / "bracket.dxf"
    assert actual.exists(), "DXF not generated"
    assert_dxf_equal(actual, get_required_path(_GOLDEN_DXF))


def test_svg_golden(export_outputs: Path) -> None:
    actual = export_outputs / "bracket.svg"
    assert actual.exists(), "SVG not generated"
    assert_svg_equal(actual, get_required_path(_GOLDEN_SVG))


def test_pdf_golden(export_outputs: Path) -> None:
    actual = export_outputs / "bracket.pdf"
    assert actual.exists(), "PDF not generated"
    assert_pdf_equal(actual, get_required_path(_GOLDEN_PDF))


if __name__ == "__main__":
    pytest_bazel.main()
