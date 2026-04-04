"""Test SVG and PDF export from parametric_rect.py in FreeCAD Docker container."""

import shutil
from pathlib import Path

import pytest_bazel

from util.bazel.runfiles import get_required_path
from util.oci import load_image
from util.testing.container_logs import LoggedContainer
from util.testing.undeclared_outputs import undeclared_outputs_dir

_IMAGE_TAG = "freecad-test:pinned"
_TARBALL = "_main/skills/freecad/freecad_test_load/tarball.tar"
_SCRIPT = "_main/skills/freecad/parametric_rect.py"


def test_export_svg_and_pdf(tmp_path: Path) -> None:
    load_image(_TARBALL)
    script = get_required_path(_SCRIPT)

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

    # Verify all three export formats were generated
    dxf = tmp_path / "rect.dxf"
    svg = tmp_path / "rect.svg"
    pdf = tmp_path / "rect.pdf"

    assert dxf.exists(), "DXF not generated"
    assert svg.exists(), "SVG not generated"
    assert pdf.exists(), "PDF not generated"

    print(f"DXF: {dxf.stat().st_size} bytes")
    print(f"SVG: {svg.stat().st_size} bytes")
    print(f"PDF: {pdf.stat().st_size} bytes")

    # SVG should be valid XML containing svg tag
    svg_content = svg.read_text()
    assert "<svg" in svg_content, "SVG file doesn't contain <svg tag"
    assert svg.stat().st_size > 100, "SVG file suspiciously small"

    # PDF should start with %PDF header
    pdf_header = pdf.read_bytes()[:5]
    assert pdf_header == b"%PDF-", f"PDF file doesn't start with %PDF- header, got {pdf_header!r}"
    assert pdf.stat().st_size > 100, "PDF file suspiciously small"

    # Save to undeclared outputs for golden file extraction
    out_dir = undeclared_outputs_dir()
    for name in ("rect.svg", "rect.pdf"):
        shutil.copy2(tmp_path / name, out_dir / name)


if __name__ == "__main__":
    pytest_bazel.main()
