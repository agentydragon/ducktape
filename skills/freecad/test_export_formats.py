"""Golden-file test: parametric rectangle exports to DXF, SVG, and PDF."""

from pathlib import Path

import pytest
import pytest_bazel

from skills.freecad.testing.compare import assert_dxf_equal, assert_pdf_equal, assert_svg_equal
from util.bazel.runfiles import get_required_path
from util.oci import load_image
from util.testing.container_logs import LoggedContainer

_IMAGE_TAG = "freecad-test:pinned"
_TARBALL = "_main/skills/freecad/freecad_test_load/tarball.tar"
_SCRIPT = "_main/skills/freecad/parametric_rect.py"
_GOLDEN_DXF = "_main/skills/freecad/golden/rect.dxf"
_GOLDEN_SVG = "_main/skills/freecad/golden/rect.svg"
_GOLDEN_PDF = "_main/skills/freecad/golden/rect.pdf"


@pytest.fixture(scope="module")
def export_outputs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run parametric_rect.py once and return the output directory."""
    load_image(_TARBALL)
    script = get_required_path(_SCRIPT)
    tmp_path = tmp_path_factory.mktemp("freecad-export")

    with LoggedContainer(
        _IMAGE_TAG,
        test_name="freecad-export-formats",
        command="sleep infinity",
        volumes=[(str(script), "/work/parametric_rect.py", "ro"), (str(tmp_path), "/output", "rw")],
        docker_client_kw={"timeout": 120},
    ) as container:
        result = container.exec(
            'bash -c "OUTDIR=/output xvfb-run -a -s \\"-screen 0 1024x768x24\\" freecadcmd /work/parametric_rect.py"'
        )
        output = result.output.decode(errors="replace")
        print(output)
        assert result.exit_code == 0, f"freecadcmd failed (exit {result.exit_code}): {output[:500]}"

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
