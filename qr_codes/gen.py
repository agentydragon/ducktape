"""Generate a QR code SVG with an optional Bebas Neue caption below."""

import argparse
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

import qrcode
from PIL import ImageFont
from qrcode.image.svg import SvgPathImage

_FONT_FAMILY = "Bebas Neue"
_SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_BORDER = 4  # quiet-zone modules

# Letter paper at 96 dpi.
_PAPER_W = 816  # 8.5in
_PAPER_H = 1056  # 11in
_TARGET_QR_W = 672  # ~7in — fills most of the page width


def _font_text_width_at_1em(text: str) -> float:
    """Return the rendered width of text in units of 1em."""
    font_path = subprocess.check_output(["fc-match", _FONT_FAMILY, "--format=%{file}"], text=True).strip()
    font = ImageFont.truetype(font_path, size=1000)
    return font.getlength(text) / 1000


def generate(text: str, output: Path, caption: str | None = None) -> None:
    qr = qrcode.QRCode(border=_BORDER, image_factory=SvgPathImage)
    qr.add_data(text)
    modules_across = len(qr.get_matrix())
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

    # Round-trip through bytes because qrcode can use either ElementTree or lxml.
    symbol = ET.fromstring(qr.make_image().to_string())
    symbol.attrib.update(width=str(size), height=str(size))
    ns = f"{{{_SVG_NAMESPACE}}}"
    ET.register_namespace("", _SVG_NAMESPACE)
    canvas = ET.Element(f"{ns}svg", width="8.5in", height="11in", viewBox=f"{vb_x} {vb_y} {_PAPER_W} {_PAPER_H}")
    ET.SubElement(canvas, f"{ns}rect", x="0", y="0", width=str(size), height=str(content_h), fill="white")
    canvas.append(symbol)
    if caption:
        ET.SubElement(
            canvas,
            f"{ns}text",
            {
                "x": str(size / 2),
                "y": str(size + font_size // 2 + box // 4),
                "text-anchor": "middle",
                "font-family": _FONT_FAMILY,
                "font-size": str(font_size),
                "fill": "black",
            },
        ).text = caption

    output.write_text(ET.tostring(canvas, encoding="unicode"), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--caption")
    args = parser.parse_args()
    generate(args.text, args.output, args.caption)


if __name__ == "__main__":
    main()
