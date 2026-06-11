"""Generate a QR code SVG with an optional Bebas Neue caption below."""

import argparse
import subprocess
from pathlib import Path

import qrcode
import svg
from fontTools.ttLib import TTFont

_FONT_FAMILY = "Bebas Neue"
_BORDER = 4  # quiet-zone modules

# Letter paper at 96 dpi.
_PAPER_W = 816  # 8.5in
_PAPER_H = 1056  # 11in
_TARGET_QR_W = 672  # ~7in — fills most of the page width


def _font_text_width_at_1em(text: str) -> float:
    """Return the advance width of text in units of 1em, using fontconfig + fonttools."""
    font_path = subprocess.check_output(["fc-match", _FONT_FAMILY, "--format=%{file}"], text=True).strip()
    font = TTFont(font_path)
    cmap = font.getBestCmap()
    hmtx = font["hmtx"].metrics
    units_per_em: int = font["head"].unitsPerEm
    if cmap is None:
        raise ValueError(f"Font {font_path} has no cmap table")
    total: int = sum(hmtx.get(cmap.get(ord(c), ".notdef"), (0, 0))[0] for c in text)
    return total / units_per_em


def generate(text: str, output: Path, caption: str | None = None) -> None:
    qr = qrcode.QRCode(border=_BORDER)
    qr.add_data(text)
    qr.make(fit=True)
    matrix: list[list[bool | None]] = qr.modules

    n = len(matrix)
    modules_across = n + _BORDER * 2
    box = _TARGET_QR_W // modules_across
    size = modules_across * box

    if caption:
        font_size = int(size / _font_text_width_at_1em(caption))
        caption_height = font_size + box // 4
    else:
        font_size = 0
        caption_height = 0

    content_h = size + caption_height

    # Center content on letter page via viewBox.
    vb_x = -(_PAPER_W - size) // 2
    vb_y = -(_PAPER_H - content_h) // 2

    elements: list[svg.Element] = []

    # White background.
    elements.append(svg.Rect(x=0, y=0, width=size, height=content_h, fill="white"))

    # QR modules.
    offset = _BORDER * box
    for row_idx, row in enumerate(matrix):
        for col_idx, dark in enumerate(row):
            if dark:
                elements.append(
                    svg.Rect(x=offset + col_idx * box, y=offset + row_idx * box, width=box, height=box, fill="black")
                )

    if caption:
        elements.append(
            svg.Text(
                text=caption,
                x=size / 2,
                y=size + font_size // 2 + box // 4,
                text_anchor="middle",
                font_family=_FONT_FAMILY,
                font_size=font_size,
                fill="black",
            )
        )

    canvas = svg.SVG(
        width=svg.Length(8.5, "in"),
        height=svg.Length(11, "in"),
        viewBox=svg.ViewBoxSpec(vb_x, vb_y, _PAPER_W, _PAPER_H),
        elements=elements,
    )
    output.write_text(str(canvas))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--caption")
    args = parser.parse_args()
    generate(args.text, args.output, args.caption)


if __name__ == "__main__":
    main()
