"""Golden-file test: parametric_rect.py → export_page.py → DXF/SVG/PDF."""

import shutil
from pathlib import Path

import pytest
import pytest_bazel

from skills.freecad.testing.compare import assert_dxf_equal, assert_pdf_equal, assert_svg_equal
from util.bazel.runfiles import get_required_path
from util.oci import load_image
from util.testing.container_logs import LoggedContainer
from util.testing.undeclared_outputs import undeclared_outputs_dir

_IMAGE_TAG = "freecad-test:pinned"
_TARBALL = "_main/skills/freecad/freecad_test_load/tarball.tar"
_PARAMETRIC_RECT = "_main/skills/freecad/parametric_rect.py"
_EXPORT_PAGE = "_main/skills/freecad/export_page.py"
_GOLDEN_DXF = "_main/skills/freecad/golden/rect.dxf"
_GOLDEN_SVG = "_main/skills/freecad/golden/rect.svg"
_GOLDEN_PDF = "_main/skills/freecad/golden/rect.pdf"

_XVFB = 'xvfb-run -a -s \\"-screen 0 1024x768x24\\"'


def _exec(container: LoggedContainer, cmd: str) -> None:
    result = container.exec(cmd)
    output = result.output.decode(errors="replace")
    print(output)
    assert result.exit_code == 0, f"Command failed (exit {result.exit_code}): {output[:500]}"


@pytest.fixture(scope="module")
def export_outputs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run parametric_rect.py then export_page.py and return the output directory."""
    load_image(_TARBALL)
    tmp_path = tmp_path_factory.mktemp("freecad-export")

    with LoggedContainer(
        _IMAGE_TAG,
        test_name="freecad-export-formats",
        command="sleep infinity",
        volumes=[
            (str(get_required_path(_PARAMETRIC_RECT)), "/work/parametric_rect.py", "ro"),
            (str(get_required_path(_EXPORT_PAGE)), "/work/export_page.py", "ro"),
            (str(tmp_path), "/output", "rw"),
        ],
        docker_client_kw={"timeout": 120},
    ) as container:
        _exec(container, f'bash -c "OUTDIR=/output {_XVFB} freecadcmd /work/parametric_rect.py"')
        _exec(container, f'bash -c "INPUT=/output/rect.FCStd OUTDIR=/output {_XVFB} freecadcmd /work/export_page.py"')

    # Save actual outputs for golden file updates
    out_dir = undeclared_outputs_dir() / "export-formats"
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in ("rect.dxf", "rect.svg", "rect.pdf"):
        src = tmp_path / f
        if src.exists():
            shutil.copy2(src, out_dir / f)

    return tmp_path


def test_dxf_golden(export_outputs: Path) -> None:
    actual = export_outputs / "rect.dxf"
    assert actual.exists(), "DXF not generated"
    assert_dxf_equal(actual, get_required_path(_GOLDEN_DXF))


def test_svg_golden(export_outputs: Path) -> None:
    actual = export_outputs / "rect.svg"
    assert actual.exists(), "SVG not generated"
    assert_svg_equal(actual, get_required_path(_GOLDEN_SVG))


def test_pdf_golden(export_outputs: Path) -> None:
    actual = export_outputs / "rect.pdf"
    assert actual.exists(), "PDF not generated"
    assert_pdf_equal(actual, get_required_path(_GOLDEN_PDF))


if __name__ == "__main__":
    pytest_bazel.main()
