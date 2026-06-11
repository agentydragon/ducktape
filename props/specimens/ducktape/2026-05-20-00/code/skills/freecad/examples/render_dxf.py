"""Render a DXF file to PNG using ezdxf's drawing API.

Usage: python3 render_dxf.py <input.dxf> <output.png> [--dpi 200]

Requires: ezdxf[draw] (which pulls in matplotlib + Pillow).
"""

import argparse
from pathlib import Path

import ezdxf
import matplotlib
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.config import BackgroundPolicy, Configuration
from ezdxf.addons.drawing.file_output import MatplotlibFileOutput


def _configure_fonts() -> None:
    """Point ezdxf at matplotlib's bundled DejaVu fonts.

    In a Bazel sandbox, system font directories (/usr/share/fonts, etc.) are
    inaccessible. matplotlib ships DejaVu TTF fonts as package data — we add
    that directory to ezdxf's support_dirs so the font manager finds them.
    """
    mpl_font_dir = str(Path(matplotlib.get_data_path()) / "fonts" / "ttf")
    if mpl_font_dir not in ezdxf.options.support_dirs:
        ezdxf.options.support_dirs = [mpl_font_dir]


def render_dxf(input_path: Path, output_path: Path, *, dpi: int = 200) -> None:
    """Render a DXF file to a PNG image."""
    _configure_fonts()
    doc = ezdxf.readfile(str(input_path))
    layout = doc.modelspace()
    ctx = RenderContext(doc)
    config = Configuration().with_changes(background_policy=BackgroundPolicy.WHITE)
    file_output = MatplotlibFileOutput(dpi)
    out = file_output.backend()
    frontend = Frontend(ctx, out, config=config)
    frontend.draw_layout(layout, finalize=True)
    file_output.save(output_path)
    print(f"Rendered: {output_path} ({output_path.stat().st_size} bytes, {dpi} dpi)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Render DXF to PNG")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--dpi", type=int, default=200)
    args = parser.parse_args()
    render_dxf(args.input, args.output, dpi=args.dpi)


if __name__ == "__main__":
    main()
