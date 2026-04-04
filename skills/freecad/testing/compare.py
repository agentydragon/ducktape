"""Golden-file comparators for FreeCAD export formats (DXF, SVG, PDF).

Each comparator normalizes non-deterministic content before comparison:
- DXF: strips version strings, timestamps, and comment lines
- SVG: sorts XML attributes (Qt emits them in non-deterministic order)
- PDF: normalizes /CreationDate timestamp
"""

import difflib
import re
from pathlib import Path
from xml.etree import ElementTree

# --- DXF ---

_DXF_STRIP_PATTERNS = re.compile(r"^\$TD(CREATE|UPDATE)|^\$VERSIONSTRING$")


def _normalize_dxf(text: str) -> list[str]:
    """Strip non-deterministic lines from DXF text."""
    lines = text.splitlines(keepends=True)
    result = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\n")
        # Group code 999 = comment: skip this line and the next (value)
        if line.strip() == "999":
            i += 2
            continue
        # Header variables that change between versions/runs
        if _DXF_STRIP_PATTERNS.match(line.strip()):
            i += 2
            continue
        result.append(lines[i])
        i += 1
    return result


def assert_dxf_equal(actual_path: Path, golden_path: Path) -> None:
    """Assert two DXF files match after normalizing non-deterministic fields."""
    actual = _normalize_dxf(actual_path.read_text())
    golden = _normalize_dxf(golden_path.read_text())
    if actual != golden:
        diff = "".join(difflib.unified_diff(golden, actual, fromfile="golden", tofile="actual", n=3))
        raise AssertionError(f"DXF mismatch:\n{diff[:500]}")


# --- SVG ---


def _normalize_svg_element(elem: ElementTree.Element) -> None:
    """Recursively sort attributes for stable comparison."""
    attribs = sorted(elem.attrib.items())
    elem.attrib.clear()
    for k, v in attribs:
        elem.attrib[k] = v
    for child in elem:
        _normalize_svg_element(child)


def _normalize_svg(text: str) -> str:
    """Parse SVG XML and re-serialize with sorted attributes for stable comparison."""
    root = ElementTree.fromstring(text)
    _normalize_svg_element(root)
    ElementTree.indent(root)
    return ElementTree.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def assert_svg_equal(actual_path: Path, golden_path: Path) -> None:
    """Assert two SVG files match after normalizing XML attribute order."""
    actual = _normalize_svg(actual_path.read_text())
    golden = _normalize_svg(golden_path.read_text())
    if actual != golden:
        diff = "".join(
            difflib.unified_diff(
                golden.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile="golden",
                tofile="actual",
                n=3,
            )
        )
        raise AssertionError(f"SVG mismatch:\n{diff[:500]}")


# --- PDF ---

_CREATION_DATE_RE = re.compile(rb"/CreationDate \(D:\d{14}Z?\)")
_CREATION_DATE_REPLACEMENT = b"/CreationDate (D:00000000000000Z)"


def _normalize_pdf(data: bytes) -> bytes:
    """Replace non-deterministic PDF metadata with fixed values."""
    return _CREATION_DATE_RE.sub(_CREATION_DATE_REPLACEMENT, data)


def assert_pdf_equal(actual_path: Path, golden_path: Path) -> None:
    """Assert two PDF files match after normalizing /CreationDate."""
    actual = _normalize_pdf(actual_path.read_bytes())
    golden = _normalize_pdf(golden_path.read_bytes())
    if actual == golden:
        return
    if len(actual) != len(golden):
        raise AssertionError(f"PDF size mismatch: golden={len(golden)} actual={len(actual)}")
    for i, (a, g) in enumerate(zip(actual, golden, strict=False)):
        if a != g:
            context = actual[max(0, i - 20) : i + 20]
            raise AssertionError(f"PDF differs at byte {i}: golden=0x{g:02x} actual=0x{a:02x}, context: {context!r}")
