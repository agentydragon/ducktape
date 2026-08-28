"""Generate LightBurn geometry reference test documents.

Creates .lbrn2 files to verify coordinate system, text anchors, rotation,
scaling, and mirror flag behavior. Open in LightBurn and export as SVG to
establish ground truth for the lbrn2_writer.

All coordinates use LightBurn's native Y-up convention: larger Y is higher
on the page, origin at bottom-left.

Usage:
    bazel run //laser/lightburn/ground_truth:generate_test_docs_bin -- /tmp/test_docs
"""

from __future__ import annotations

import argparse
from pathlib import Path

from laser.lightburn.lbrn2_writer import (
    AnyShape,
    CutMode,
    CutSetting,
    HAlign,
    LightBurnProject,
    RectShape,
    TextShape,
    VAlign,
    XForm,
)

_TEXT_H = 5.0  # mm, default text height
_LAYER_TEXT = 0
_LAYER_MARKER = 1


def _default_layers() -> list[CutSetting]:
    return [
        CutSetting(index=_LAYER_TEXT, name="Text", mode=CutMode.CUT, min_power=10, max_power=10, speed=200),
        CutSetting(index=_LAYER_MARKER, name="Marker", mode=CutMode.CUT, min_power=10, max_power=10, speed=200),
    ]


def _marker(x: float, y: float) -> list[AnyShape]:
    """Small crosshair: two crossing thin rects centred at (x, y)."""
    arm = 8.0
    thickness = 0.2
    return [
        RectShape(cut_index=_LAYER_MARKER, width=arm, height=thickness, xform=XForm.translate(x, y)),
        RectShape(cut_index=_LAYER_MARKER, width=thickness, height=arm, xform=XForm.translate(x, y)),
    ]


def _label(text: str, x: float, y: float, ah: HAlign = HAlign.LEFT, av: VAlign = VAlign.TOP) -> TextShape:
    return TextShape(cut_index=_LAYER_TEXT, text=text, height=_TEXT_H, xform=XForm.translate(x, y), ah=ah, av=av)


def _labeled_marker(x: float, y: float) -> list[AnyShape]:
    """Crosshair at (x, y) with a coordinate label offset right and below."""
    return [*_marker(x, y), _label(str((x, y)), x + 3, y - 3)]


def _section_label(text: str, x: float, y: float) -> TextShape:
    """Bold section header at (x, y)."""
    return TextShape(
        cut_index=_LAYER_TEXT,
        text=text,
        height=7.0,
        xform=XForm.translate(x, y),
        ah=HAlign.LEFT,
        av=VAlign.TOP,
        bold=True,
    )


def _write(project: LightBurnProject, path: Path) -> None:
    path.write_text(project.to_xml_str(), encoding="utf-8")
    print(f"  {path}")


# ── Test 1: Anchors (non-rotated + rotated) ──────────────────────────────────


def gen_anchors(out_dir: Path) -> None:
    """3x3 grid of HAlign x VAlign at crosshairs, plain and rotated 90 CCW."""
    shapes: list[AnyShape] = []
    spacing = 40.0
    h_aligns = [HAlign.LEFT, HAlign.CENTER, HAlign.RIGHT]
    v_aligns = [VAlign.TOP, VAlign.CENTER, VAlign.BOTTOM]
    h_names = {HAlign.LEFT: "L", HAlign.CENTER: "C", HAlign.RIGHT: "R"}
    v_names = {VAlign.TOP: "T", VAlign.CENTER: "C", VAlign.BOTTOM: "B"}

    # Section 1: non-rotated (left half of page)
    # Header at top (y=130), body rows descend below it.
    ox = 30.0
    y_header = 130.0
    y_body = y_header - 15.0
    shapes.append(_section_label("Non-rotated", ox, y_header))
    for col, ah in enumerate(h_aligns):
        for row, av in enumerate(v_aligns):
            x = ox + col * spacing
            y = y_body - row * spacing
            shapes.extend(_marker(x, y))
            shapes.append(
                TextShape(
                    cut_index=_LAYER_TEXT,
                    text=f"{h_names[ah]},{v_names[av]}",
                    height=_TEXT_H,
                    xform=XForm.translate(x, y),
                    ah=ah,
                    av=av,
                )
            )

    # Section 2: rotated 90° CCW (right half of page)
    ox2 = ox + 3 * spacing + 30.0
    shapes.append(_section_label("Rotated 90 CCW", ox2, y_header))
    for col, ah in enumerate(h_aligns):
        for row, av in enumerate(v_aligns):
            x = ox2 + col * spacing
            y = y_body - row * spacing
            shapes.extend(_marker(x, y))
            shapes.append(
                TextShape(
                    cut_index=_LAYER_TEXT,
                    text=f"{h_names[ah]},{v_names[av]}",
                    height=_TEXT_H,
                    xform=XForm.rotate90ccw(x, y),
                    ah=ah,
                    av=av,
                )
            )

    project = LightBurnProject(
        cut_settings=_default_layers(),
        shapes=shapes,
        notes="Text anchor test: non-rotated (left) and rotated 90 CCW (right). "
        "Each HAlign x VAlign combo at its own crosshair.",
        mirror_x=False,
        mirror_y=False,
    )
    _write(project, out_dir / "test_anchors.lbrn2")


# ── Test 2: XForm (coordinates, rotation, scale) ─────────────────────────────


def gen_xform(out_dir: Path) -> None:
    """Coordinates, rotation, scale/mirror via XForm, layer colors."""
    shapes: list[AnyShape] = []

    # Page layout: section headers at top (large Y), content descends below.
    y_header = 135.0
    y_body = 120.0

    # Column 1 (left): Coordinates + negative coordinates
    col1 = 10.0
    shapes.append(_section_label("Coordinates", col1, y_header))
    for x, y in [(10.0, y_body), (60.0, y_body), (10.0, y_body - 30), (-20.0, y_body - 60)]:
        shapes.extend(_labeled_marker(x, y))

    # Column 2 (middle): Rotation
    col2 = 130.0
    cx, cy = col2 + 40, y_body - 40
    shapes.append(_section_label("Rotation", col2, y_header))
    shapes.extend(_marker(cx, cy))
    for angle, label in [(0, "0deg"), (45, "45 CCW"), (90, "90 CCW"), (-90, "90 CW"), (180, "180")]:
        shapes.append(
            TextShape(
                cut_index=_LAYER_TEXT,
                text=f"-> {label}",
                height=_TEXT_H,
                xform=XForm.rotate(angle, cx, cy),
                ah=HAlign.LEFT,
                av=VAlign.CENTER,
            )
        )

    # Columns 3-4 (right): Scale / mirror — two sub-columns of 3 rows
    col3 = 230.0
    col4 = 290.0
    shapes.append(_section_label("Scale / mirror", col3, y_header))
    row_spacing = 25.0
    scale_cases = [
        # Left sub-column: rows descend from y_body
        (XForm(a=1, b=0, c=0, d=1, tx=col3, ty=y_body), "identity"),
        (XForm(a=2, b=0, c=0, d=1, tx=col3, ty=y_body - row_spacing), "Sx=2"),
        (XForm(a=1, b=0, c=0, d=2, tx=col3, ty=y_body - 2 * row_spacing), "Sy=2"),
        # Right sub-column
        (XForm(a=2, b=0, c=0, d=2, tx=col4, ty=y_body), "Sx=2,Sy=2"),
        (XForm(a=-1, b=0, c=0, d=1, tx=col4, ty=y_body - row_spacing), "Sx=-1 (X flip)"),
        (XForm(a=1, b=0, c=0, d=-1, tx=col4, ty=y_body - 2 * row_spacing), "Sy=-1 (Y flip)"),
    ]
    for xform, label in scale_cases:
        shapes.append(RectShape(cut_index=_LAYER_MARKER, width=20, height=10, xform=xform))
        shapes.append(
            TextShape(cut_index=_LAYER_TEXT, text="Fd", height=4.0, xform=xform, ah=HAlign.CENTER, av=VAlign.CENTER)
        )
        shapes.append(_label(label, xform.tx + 15, xform.ty))

    # Bottom section: Layer colors (low Y = bottom of page)
    n_color_layers = 10
    color_layers = [
        CutSetting(index=i, name=f"L{i}", mode=CutMode.CUT, min_power=10, max_power=10, speed=200)
        for i in range(n_color_layers)
    ]
    color_y = 15.0
    shapes.append(_section_label("Layer colors (0-9)", col1, color_y + 15))
    for i in range(n_color_layers):
        x = col1 + 5 + i * 15.0
        shapes.append(RectShape(cut_index=i, width=12, height=12, xform=XForm.translate(x, color_y)))
        shapes.append(
            TextShape(
                cut_index=i,
                text=str(i),
                height=4.0,
                xform=XForm.translate(x, color_y),
                ah=HAlign.CENTER,
                av=VAlign.CENTER,
            )
        )

    project = LightBurnProject(
        cut_settings=color_layers,
        shapes=shapes,
        notes="XForm test: coordinates (left), rotation (middle), scale/mirror (right), layer colors (bottom).",
        mirror_x=False,
        mirror_y=False,
    )
    _write(project, out_dir / "test_xform.lbrn2")


# ── Test 3: MirrorX/MirrorY root attributes ──────────────────────────────────


def _mirror_test_content() -> tuple[list[CutSetting], list[AnyShape]]:
    """Asymmetric marker pattern for mirror flag testing."""
    shapes: list[AnyShape] = []
    # Two markers at top (y=50), one at bottom-left (y=20) — an L-shape.
    for x, y in [(20, 50), (70, 50), (20, 20)]:
        shapes.extend(_labeled_marker(x, y))
    return _default_layers(), shapes


def gen_mirror_flags(out_dir: Path) -> None:
    layers, shapes = _mirror_test_content()
    _write(
        LightBurnProject(
            cut_settings=layers,
            shapes=shapes,
            notes="Mirror test: MirrorX=False, MirrorY=False",
            mirror_x=False,
            mirror_y=False,
        ),
        out_dir / "test_mirror_off.lbrn2",
    )

    layers2, shapes2 = _mirror_test_content()
    _write(
        LightBurnProject(
            cut_settings=layers2,
            shapes=shapes2,
            notes="Mirror test: MirrorX=True, MirrorY=True",
            mirror_x=True,
            mirror_y=True,
        ),
        out_dir / "test_mirror_on.lbrn2",
    )


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("output_dir", metavar="DIR", help="Directory to write .lbrn2 files into")
    args = p.parse_args(argv)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Generating test documents:")
    gen_anchors(out_dir)
    gen_xform(out_dir)
    gen_mirror_flags(out_dir)
    print("Done.")


if __name__ == "__main__":
    main()
