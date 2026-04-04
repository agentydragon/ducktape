"""Compare SVG files with normalized XML attribute order."""

import difflib
from pathlib import Path
from xml.etree import ElementTree


def _normalize_element(elem: ElementTree.Element) -> None:
    """Recursively sort attributes and child elements for stable comparison."""
    # Sort attributes by key (ElementTree preserves insertion order, which is non-deterministic)
    attribs = sorted(elem.attrib.items())
    elem.attrib.clear()
    for k, v in attribs:
        elem.attrib[k] = v
    for child in elem:
        _normalize_element(child)


def normalize_svg(text: str) -> str:
    """Parse SVG XML and re-serialize with sorted attributes for stable comparison."""
    root = ElementTree.fromstring(text)
    _normalize_element(root)
    ElementTree.indent(root)
    return ElementTree.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def compare_svg_files(actual_path: Path, golden_path: Path) -> str | None:
    """Compare two SVG files after normalization. Returns None if match, diff string if mismatch."""
    actual = normalize_svg(actual_path.read_text())
    golden = normalize_svg(golden_path.read_text())
    if actual == golden:
        return None
    return "".join(
        difflib.unified_diff(
            golden.splitlines(keepends=True), actual.splitlines(keepends=True), fromfile="golden", tofile="actual", n=3
        )
    )
