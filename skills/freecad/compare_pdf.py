"""Compare PDF files, normalizing non-deterministic metadata."""

import re
from pathlib import Path

# /CreationDate (D:20260404104042Z) — timestamp changes every run
_CREATION_DATE_RE = re.compile(rb"/CreationDate \(D:\d{14}Z?\)")
_CREATION_DATE_REPLACEMENT = b"/CreationDate (D:00000000000000Z)"


def normalize_pdf(data: bytes) -> bytes:
    """Replace non-deterministic PDF metadata with fixed values."""
    return _CREATION_DATE_RE.sub(_CREATION_DATE_REPLACEMENT, data)


def compare_pdf_files(actual_path: Path, golden_path: Path) -> str | None:
    """Compare two PDF files after normalization. Returns None if match, diff description if mismatch."""
    actual = normalize_pdf(actual_path.read_bytes())
    golden = normalize_pdf(golden_path.read_bytes())
    if actual == golden:
        return None
    if len(actual) != len(golden):
        return f"Size mismatch: golden={len(golden)} actual={len(actual)}"
    # Find first differing byte
    for i, (a, g) in enumerate(zip(actual, golden, strict=False)):
        if a != g:
            context = actual[max(0, i - 20) : i + 20]
            return f"First difference at byte {i}: golden=0x{g:02x} actual=0x{a:02x}, context: {context!r}"
    return "Unknown difference"
