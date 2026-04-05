"""Test: FreeCAD AppImage creates a rectangle DXF without Docker."""

import tempfile
from pathlib import Path

import pytest_bazel

from util.bazel.runfiles import get_required_path

_RECT_SCRIPT = "_main/skills/freecad/explore_appimage/rect.py"


def test_rect_dxf(freecad_run) -> None:
    script = get_required_path(_RECT_SCRIPT)
    with tempfile.TemporaryDirectory() as outdir:
        result = freecad_run(script, Path(outdir))
        assert result.returncode == 0, (
            f"freecadcmd failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout[:500]}\n"
            f"stderr: {result.stderr[:500]}"
        )

        dxf = Path(outdir) / "rect.dxf"
        assert dxf.exists(), "rect.dxf not produced"
        assert dxf.stat().st_size > 100

        content = dxf.read_text()
        assert "LINE" in content, "Expected LINE entities in DXF"
        assert content.count("LINE") >= 4, "Expected at least 4 LINE entities for rectangle"


if __name__ == "__main__":
    pytest_bazel.main()
