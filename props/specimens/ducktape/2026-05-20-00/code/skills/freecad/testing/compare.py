"""Golden-file comparators for FreeCAD export formats (DXF, SVG, PDF, PNG).

Each comparator normalizes non-deterministic content before comparison.
All assert functions check that the actual file exists, and wrap the
comparison in an OTel span for profiling.
"""

import difflib
import re
from pathlib import Path
from xml.etree import ElementTree

from opentelemetry import trace
from PIL import Image
from pypdf import PdfReader

tracer = trace.get_tracer(__name__)

# --- DXF ---

_DXF_STRIP_PATTERNS = re.compile(r"^\$TD(CREATE|UPDATE)|^\$VERSIONSTRING$")
_DXF_FLOAT_RE = re.compile(r"^[- ]?\d*\.?\d+([eE][+-]?\d+)?$")


def _normalize_dxf_value(line: str) -> str:
    """Normalize floating-point values: snap near-zero to 0, round to 6 decimals."""
    stripped = line.strip()
    if not _DXF_FLOAT_RE.match(stripped):
        return line
    try:
        val = float(stripped)
    except ValueError:
        return line
    if abs(val) < 1e-9:
        val = 0.0
    # Round to 6 significant decimals to absorb FP drift across runs
    normalized = f"{val:.6f}".rstrip("0").rstrip(".")
    # Preserve original line ending
    ending = line[len(line.rstrip()) :]
    return normalized + ending


def _normalize_dxf(text: str) -> list[str]:
    """Strip non-deterministic lines and normalize floats in DXF text."""
    lines = text.splitlines(keepends=True)
    result = []
    skip_next = False
    for line in lines:
        if skip_next:
            skip_next = False
            continue
        stripped = line.rstrip("\n").strip()
        # DXF group code 999 = comment, or header vars that change between runs.
        # These are key-value pairs: skip this line (key) and the next (value).
        if stripped == "999" or _DXF_STRIP_PATTERNS.match(stripped):
            skip_next = True
            continue
        result.append(_normalize_dxf_value(line))
    return result


def assert_dxf_equal(actual_path: Path, golden_path: Path) -> None:
    """Assert two DXF files match after normalizing non-deterministic fields."""
    with tracer.start_as_current_span("assert_dxf_equal"):
        assert actual_path.exists(), f"DXF not generated: {actual_path}"
        actual = _normalize_dxf(actual_path.read_text())
        golden = _normalize_dxf(golden_path.read_text())
        if actual != golden:
            diff = "".join(difflib.unified_diff(golden, actual, fromfile="golden", tofile="actual", n=3))
            raise AssertionError(f"DXF mismatch:\n{diff[:500]}")


# --- SVG ---


def _normalize_svg_element(elem: ElementTree.Element) -> None:
    """Recursively sort attributes and child elements for stable comparison.

    Qt emits SVG attributes in non-deterministic order, and glyph path elements
    within text groups can also appear in varying order between runs. Sorting
    both attributes and child elements produces a canonical form.
    """
    attribs = sorted(elem.attrib.items())
    elem.attrib.clear()
    for k, v in attribs:
        elem.attrib[k] = v
    for child in elem:
        _normalize_svg_element(child)
    # Sort child elements by their serialized form for deterministic order
    children = list(elem)
    if children:
        for child in children:
            elem.remove(child)
        children.sort(key=lambda c: ElementTree.tostring(c, encoding="unicode"))
        for child in children:
            elem.append(child)


def _normalize_svg(text: str) -> str:
    """Parse SVG XML and re-serialize with sorted attributes for stable comparison."""
    root = ElementTree.fromstring(text)
    _normalize_svg_element(root)
    ElementTree.indent(root)
    return ElementTree.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def assert_svg_equal(actual_path: Path, golden_path: Path) -> None:
    """Assert two SVG files match after normalizing XML attribute order."""
    with tracer.start_as_current_span("assert_svg_equal"):
        assert actual_path.exists(), f"SVG not generated: {actual_path}"
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


# Font names in embedded PDFs are non-deterministic: subset prefix varies (e.g., QNAAAA+)
# and the font itself may differ across workers (DejaVuSans vs osifont).
_FONT_NAME_RE = re.compile(rb"/FontName /[A-Z]{6}\+\S+")
_FONT_NAME_REPLACEMENT = b"/FontName /AAAAAA+NormalizedFont"


# PDF date strings appear in two formats:
# 1. PDF Info dict: (D:YYYYMMDDHHmmSS+TZ) — variable-length timezone suffix
# 2. XMP metadata: ISO 8601 in XML attributes (2026-04-05T04:08:38+00:00)
_PDF_DATE_RE = re.compile(rb"\(D:\d{14}[^)]*\)")
_PDF_DATE_REPLACEMENT = b"(D:20000101000000+00'00')"
_XMP_DATE_RE = re.compile(rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}")
_XMP_DATE_REPLACEMENT = b"2000-01-01T00:00:00+00:00"
# UUIDs in XMP metadata (xmpMM:DocumentID, xmpMM:InstanceID)
_UUID_RE = re.compile(rb"uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_UUID_REPLACEMENT = b"uuid:00000000-0000-0000-0000-000000000000"


# Matches floating-point numbers in PDF content streams (e.g., "247.999999", "120.0")
_PDF_FLOAT_RE = re.compile(rb"-?\d+\.\d+")


def _normalize_pdf_float(m: re.Match[bytes]) -> bytes:
    """Round floats to 4 decimal places and strip trailing zeros."""
    val = float(m.group())
    if abs(val) < 1e-6:
        val = 0.0
    return f"{val:.4f}".rstrip("0").rstrip(".").encode()


def _strip_pdf_metadata(pdf_path: Path) -> bytes:
    """Extract and normalize PDF page content for deterministic comparison.

    FreeCAD TechDraw PDFs have several sources of non-determinism:
    - /Info dict and /Metadata XMP: timestamps, UUIDs, producer strings
    - Font subset prefixes: random 6-char prefix (e.g., QNAAAA+osifont)
    - FlateDecode streams: zlib compression varies between runs
    - Floating-point precision: e.g., 247.999999 vs 248

    We extract decompressed page content and normalize all non-deterministic
    tokens for stable comparison across runs.
    """
    reader = PdfReader(pdf_path)
    parts: list[bytes] = []
    for page in reader.pages:
        # extract_text() loses layout info; instead get the raw content
        # stream operators after decompression
        content = page.get_contents()
        raw = content.get_data() if content is not None else b""
        # Normalize font subset prefixes
        raw = _FONT_NAME_RE.sub(_FONT_NAME_REPLACEMENT, raw)
        # Normalize floating-point precision (247.999999 → 248)
        raw = _PDF_FLOAT_RE.sub(_normalize_pdf_float, raw)
        # Sort graphics state blocks (q...Q pairs) for deterministic order.
        # TechDraw emits drawing operations in non-deterministic order for
        # text glyphs and view projections — same content, different sequence.
        lines = raw.split(b"\n")
        blocks: list[list[bytes]] = [[]]
        for line in lines:
            blocks[-1].append(line)
            if line.strip() == b"Q":
                blocks.append([])
        sorted_blocks = sorted(blocks, key=b"".join)
        raw = b"\n".join(line for block in sorted_blocks for line in block)
        parts.append(raw)
    result = b"\n---PAGE---\n".join(parts)
    # Normalize dates and UUIDs that may appear in any remaining metadata
    result = _PDF_DATE_RE.sub(_PDF_DATE_REPLACEMENT, result)
    result = _XMP_DATE_RE.sub(_XMP_DATE_REPLACEMENT, result)
    return _UUID_RE.sub(_UUID_REPLACEMENT, result)


def assert_pdf_equal(actual_path: Path, golden_path: Path) -> None:
    """Assert two PDF files match after stripping non-deterministic metadata."""
    with tracer.start_as_current_span("assert_pdf_equal"):
        assert actual_path.exists(), f"PDF not generated: {actual_path}"
        actual = _strip_pdf_metadata(actual_path)
        golden = _strip_pdf_metadata(golden_path)
        if actual == golden:
            return
        # Find and report first difference with context
        for i in range(min(len(actual), len(golden))):
            if actual[i] != golden[i]:
                ctx_a = actual[max(0, i - 60) : i + 60]
                ctx_g = golden[max(0, i - 60) : i + 60]
                raise AssertionError(
                    f"PDF differs at byte {i} (golden={len(golden)}, actual={len(actual)}):\n"
                    f"  golden: {ctx_g!r}\n"
                    f"  actual: {ctx_a!r}"
                )
        raise AssertionError(f"PDF size mismatch: golden={len(golden)} actual={len(actual)}")


# --- PNG ---


def assert_png_equal(actual_path: Path, golden_path: Path, max_diff_fraction: float = 0.02) -> None:
    """Assert two PNG images match within a pixel channel diff tolerance."""
    with tracer.start_as_current_span("assert_png_equal"):
        assert actual_path.exists(), f"PNG not generated: {actual_path}"
        actual = Image.open(actual_path).convert("RGB")
        golden = Image.open(golden_path).convert("RGB")
        if actual.size != golden.size:
            raise AssertionError(f"Size mismatch: {actual.size} vs {golden.size}")
        a_data = actual.tobytes()
        g_data = golden.tobytes()
        differing = sum(1 for a, g in zip(a_data, g_data, strict=True) if a != g)
        diff_fraction = differing / len(a_data)
        if diff_fraction > max_diff_fraction:
            raise AssertionError(
                f"Rendered PNG differs from golden by {diff_fraction:.1%} (threshold {max_diff_fraction:.1%})"
            )
