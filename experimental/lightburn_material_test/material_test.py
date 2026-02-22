"""Material test grid generator for LightBurn (.lbrn2).

Reads a TOML configuration file that describes the grid parameters, then
generates a parametric material test grid as a .lbrn2 file.

Usage:
    python material_test.py config.toml [-o output.lbrn2]

See SPEC.md for full documentation and example_config.toml for a complete
annotated example configuration.
"""

from __future__ import annotations

import argparse
import tomllib
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

from experimental.lightburn_material_test.lbrn2_writer import (
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

# ── Cut parameter enum ─────────────────────────────────────────────────────────


class CutParam(StrEnum):
    """Named laser cut parameters that can be scanned or held constant."""

    POWER = "power"  # sets both min_power and max_power simultaneously
    POWER_MIN = "power_min"
    POWER_MAX = "power_max"
    SPEED = "speed"
    KERF = "kerf"
    Z_OFFSET = "z_offset"
    Z_PER_PASS = "z_per_pass"
    NUM_PASSES = "num_passes"


# (label, unit) for each cut parameter
_PARAM: dict[CutParam, tuple[str, str]] = {
    CutParam.POWER: ("Power", "%"),
    CutParam.POWER_MIN: ("Power min", "%"),
    CutParam.POWER_MAX: ("Power max", "%"),
    CutParam.SPEED: ("Speed", "mm/s"),
    CutParam.KERF: ("Kerf", "mm"),
    CutParam.Z_OFFSET: ("Z", "mm"),
    CutParam.Z_PER_PASS: ("Z/pass", "mm"),
    CutParam.NUM_PASSES: ("Passes", ""),
}


def fmt_val(v: float) -> str:
    """Format a parameter value for human display (no trailing zeros)."""
    if v == int(v):
        return str(int(v))
    return f"{v:.4g}"


# ── Pydantic config models ─────────────────────────────────────────────────────


class AxisConfig(BaseModel):
    """Configuration for one grid axis (X or Y)."""

    model_config = ConfigDict(extra="forbid")

    param: CutParam
    values: list[float]
    label: str | None = None  # None = auto-generate from param name + unit; "" = no label
    show_annotations: bool = True


class CutConfig(BaseModel):
    """Laser cut parameters held constant across the entire grid.

    The x/y axis parameters override their respective entries here for each cell.

    Use 'power' to set both min and max simultaneously.
    """

    model_config = ConfigDict(extra="forbid")

    power: float | None = None  # shorthand: sets both power_min and power_max
    power_min: float = 80.0  # %
    power_max: float = 80.0  # %
    speed: float = 100.0  # mm/s
    kerf: float = 0.0  # mm
    z_offset: float = 0.0  # mm, initial Z offset
    z_per_pass: float = 0.0  # mm, Z step per pass (negative = deeper)
    num_passes: int = 1

    @model_validator(mode="after")
    def apply_power_shorthand(self) -> CutConfig:
        if self.power is not None:
            self.power_min = self.power
            self.power_max = self.power
        return self

    def to_cut_setting(self, index: int, name: str) -> CutSetting:
        return CutSetting(
            index=index,
            name=name,
            min_power=self.power_min,
            max_power=self.power_max,
            speed=self.speed,
            kerf=self.kerf,
            z_offset=self.z_offset,
            z_per_pass=self.z_per_pass,
            num_passes=self.num_passes,
        )


class GeometryConfig(BaseModel):
    """Physical dimensions of the test grid cells."""

    model_config = ConfigDict(extra="forbid")

    cell_size: float = 15.0  # mm, square side length
    gap: float = 8.0  # mm, gap between adjacent cells


class AnnotationConfig(BaseModel):
    """In-cell text annotations."""

    model_config = ConfigDict(extra="forbid")

    show_cell_text: bool = False  # print param values inside each cell


class BorderConfig(BaseModel):
    """Optional border rectangle drawn around the entire grid."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    padding: float = 3.0  # mm outside the grid cells
    power: float = 10.0  # %
    speed: float = 200.0  # mm/s


class TextLayerConfig(BaseModel):
    """Cut settings for the annotation text layer (layer 0)."""

    model_config = ConfigDict(extra="forbid")

    power: float = 15.0  # %
    speed: float = 200.0  # mm/s


class FontConfig(BaseModel):
    """Font and text size configuration."""

    model_config = ConfigDict(extra="forbid")

    name: str = "Arial"
    h_title: float = 10.0  # mm, title text height
    h_subtitle: float = 7.0  # mm
    h_label: float = 6.0  # mm, axis label
    h_value: float = 5.0  # mm, axis value annotations
    h_cell: float = 4.0  # mm, in-cell parameter text


class GridConfig(BaseModel):
    """Root configuration for the material test grid.

    Corresponds to a single TOML config file.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = ""
    subtitle: str = ""  # extra text prepended to the auto-generated subtitle
    auto_subtitle: bool = True  # append constant-param summary to subtitle

    x: AxisConfig
    y: AxisConfig
    cut: CutConfig = CutConfig()
    geometry: GeometryConfig = GeometryConfig()
    annotations: AnnotationConfig = AnnotationConfig()
    border: BorderConfig = BorderConfig()
    text_layer: TextLayerConfig = TextLayerConfig()
    font: FontConfig = FontConfig()

    @model_validator(mode="after")
    def validate_axes(self) -> GridConfig:
        if self.x.param == self.y.param:
            raise ValueError(f"x.param and y.param must be different; both are {self.x.param!r}")
        return self


# ── Parameter helpers ──────────────────────────────────────────────────────────


def _apply_param(cut: CutSetting, param: CutParam, value: float) -> None:
    """Apply a named parameter value to a CutSetting."""
    if param == CutParam.POWER:
        cut.min_power = value
        cut.max_power = value
    elif param == CutParam.POWER_MIN:
        cut.min_power = value
    elif param == CutParam.POWER_MAX:
        cut.max_power = value
    elif param == CutParam.SPEED:
        cut.speed = value
    elif param == CutParam.KERF:
        cut.kerf = value
    elif param == CutParam.Z_OFFSET:
        cut.z_offset = value
    elif param == CutParam.Z_PER_PASS:
        cut.z_per_pass = value
    elif param == CutParam.NUM_PASSES:
        cut.num_passes = round(value)


def _get_param(cut: CutSetting, param: CutParam) -> float:
    """Read a named parameter value from a CutSetting."""
    if param in (CutParam.POWER, CutParam.POWER_MAX):
        return cut.max_power
    if param == CutParam.POWER_MIN:
        return cut.min_power
    if param == CutParam.SPEED:
        return cut.speed
    if param == CutParam.KERF:
        return cut.kerf
    if param == CutParam.Z_OFFSET:
        return cut.z_offset
    if param == CutParam.Z_PER_PASS:
        return cut.z_per_pass
    if param == CutParam.NUM_PASSES:
        return float(cut.num_passes)
    raise ValueError(f"Unknown param: {param!r}")  # unreachable with enum


def _auto_subtitle(config: GridConfig) -> str:
    """Build a subtitle listing the parameters constant across all cells."""
    varied: set[CutParam] = {config.x.param, config.y.param}
    if CutParam.POWER in varied:
        varied |= {CutParam.POWER_MIN, CutParam.POWER_MAX}
    if CutParam.POWER_MIN in varied or CutParam.POWER_MAX in varied:
        varied.add(CutParam.POWER)

    base = config.cut.to_cut_setting(0, "")
    parts: list[str] = []
    for param in [
        CutParam.Z_OFFSET,
        CutParam.SPEED,
        CutParam.KERF,
        CutParam.Z_PER_PASS,
        CutParam.NUM_PASSES,
        CutParam.POWER,
    ]:
        if param in varied:
            continue
        v = _get_param(base, param)
        if param == CutParam.NUM_PASSES and v == 1:
            continue
        label, unit = _PARAM[param]
        parts.append(f"{label}={fmt_val(v)}{' ' + unit if unit else ''}")
    return ", ".join(parts)


def _full_subtitle(config: GridConfig) -> str:
    pieces: list[str] = []
    if config.subtitle:
        pieces.append(config.subtitle)
    if config.auto_subtitle:
        auto = _auto_subtitle(config)
        if auto:
            pieces.append(auto)
    return ", ".join(pieces)


def _auto_label(param: CutParam) -> str:
    """Build an axis label from a CutParam (e.g. POWER_MAX → 'Power max [%]')."""
    label, unit = _PARAM[param]
    return f"{label} [{unit}]" if unit else label


# ── Layout constants ───────────────────────────────────────────────────────────

_SPACING = 3.0  # mm between text elements
_MARGIN_LEFT = 5.0  # mm left of the Y-axis label
_MARGIN_TOP = 5.0  # mm above the title
_TEXT_LAYER = 0  # layer index reserved for annotation text


def _estimate_text_width(text: str, height: float) -> float:
    """Rough rendered text width estimate (approx 0.55 x height per char)."""
    return len(text) * height * 0.55


# ── Grid generation ────────────────────────────────────────────────────────────


def generate(config: GridConfig) -> LightBurnProject:
    """Build a LightBurnProject from the grid configuration."""
    n_cols = len(config.x.values)
    n_rows = len(config.y.values)
    stride = config.geometry.cell_size + config.geometry.gap
    font = config.font

    cut_settings: list[CutSetting] = []
    shapes: list[AnyShape] = []

    # Layer 0: annotation text
    cut_settings.append(
        CutSetting(
            index=_TEXT_LAYER,
            name="Text",
            mode=CutMode.CUT,
            min_power=config.text_layer.power,
            max_power=config.text_layer.power,
            speed=config.text_layer.speed,
        )
    )

    # Layers 1..N*M: one per grid cell
    cell_cut_index: dict[tuple[int, int], int] = {}
    next_index = 1
    for row_i, y_val in enumerate(config.y.values):
        for col_j, x_val in enumerate(config.x.values):
            cut = config.cut.to_cut_setting(next_index, f"C{next_index:02d}")
            _apply_param(cut, config.x.param, x_val)
            _apply_param(cut, config.y.param, y_val)
            cut_settings.append(cut)
            cell_cut_index[(row_i, col_j)] = next_index
            next_index += 1

    # Optional border layer
    border_layer_index: int | None = None
    if config.border.enabled:
        cut_settings.append(
            CutSetting(
                index=next_index,
                name="Border",
                mode=CutMode.CUT,
                min_power=config.border.power,
                max_power=config.border.power,
                speed=config.border.speed,
            )
        )
        border_layer_index = next_index
        next_index += 1

    # ── Horizontal layout ─────────────────────────────────────────────────────
    if config.x.show_annotations and config.y.show_annotations:
        max_y_val_str = max((fmt_val(v) for v in config.y.values), key=len, default="0")
        max_y_val_width = _estimate_text_width(max_y_val_str, font.h_value)
        x_y_label_cx = _MARGIN_LEFT + font.h_label / 2.0
        x_y_val_right = _MARGIN_LEFT + font.h_label + _SPACING + max_y_val_width
        x_grid_left = x_y_val_right + _SPACING
    elif config.y.show_annotations:
        max_y_val_str = max((fmt_val(v) for v in config.y.values), key=len, default="0")
        max_y_val_width = _estimate_text_width(max_y_val_str, font.h_value)
        x_y_label_cx = _MARGIN_LEFT
        x_y_val_right = _MARGIN_LEFT + max_y_val_width
        x_grid_left = x_y_val_right + _SPACING
    else:
        x_y_label_cx = _MARGIN_LEFT
        x_y_val_right = _MARGIN_LEFT
        x_grid_left = _MARGIN_LEFT

    x_col = [x_grid_left + config.geometry.cell_size / 2.0 + col_j * stride for col_j in range(n_cols)]
    x_grid_right = x_grid_left + n_cols * stride - config.geometry.gap
    x_grid_centre = (x_grid_left + x_grid_right) / 2.0

    # ── Vertical layout (Y increases downward) ────────────────────────────────
    y = _MARGIN_TOP

    def add_text(
        text: str,
        x: float,
        y_pos: float,
        height: float,
        ah: HAlign = HAlign.LEFT,
        av: VAlign = VAlign.TOP,
        rotate90ccw: bool = False,
    ) -> None:
        xform = XForm.rotate90ccw(x, y_pos) if rotate90ccw else XForm.translate(x, y_pos)
        shapes.append(
            TextShape(cut_index=_TEXT_LAYER, text=text, height=height, xform=xform, font=font.name, ah=ah, av=av)
        )

    if config.title:
        add_text(config.title, x_grid_centre, y, font.h_title, ah=HAlign.CENTER)
        y += font.h_title + _SPACING

    subtitle_text = _full_subtitle(config)
    if subtitle_text:
        add_text(subtitle_text, x_grid_centre, y, font.h_subtitle, ah=HAlign.CENTER)
        y += font.h_subtitle + _SPACING

    x_label = config.x.label if config.x.label is not None else _auto_label(config.x.param)
    y_label = config.y.label if config.y.label is not None else _auto_label(config.y.param)

    if config.x.show_annotations:
        for col_j, x_val in enumerate(config.x.values):
            add_text(fmt_val(x_val), x_col[col_j], y, font.h_value, ah=HAlign.CENTER, av=VAlign.TOP)
        y += font.h_value + _SPACING

        if x_label:
            add_text(x_label, x_grid_centre, y, font.h_label, ah=HAlign.CENTER)
            y += font.h_label + _SPACING

    y_grid_top = y
    y_row = [y_grid_top + config.geometry.cell_size / 2.0 + row_i * stride for row_i in range(n_rows)]
    y_grid_bottom = y_grid_top + n_rows * stride - config.geometry.gap
    y_grid_centre = (y_grid_top + y_grid_bottom) / 2.0

    # Y-axis label (rotated 90° CCW, reads bottom-to-top)
    if config.y.show_annotations and y_label:
        add_text(
            y_label, x_y_label_cx, y_grid_centre, font.h_label, ah=HAlign.CENTER, av=VAlign.CENTER, rotate90ccw=True
        )

    # Y-axis value annotations (right-aligned, centred vertically on each row)
    if config.y.show_annotations:
        for row_i, y_val in enumerate(config.y.values):
            add_text(fmt_val(y_val), x_y_val_right, y_row[row_i], font.h_value, ah=HAlign.RIGHT, av=VAlign.CENTER)

    # Grid cells
    for row_i, y_val in enumerate(config.y.values):
        for col_j, x_val in enumerate(config.x.values):
            cut_idx = cell_cut_index[(row_i, col_j)]
            cx = x_col[col_j]
            cy = y_row[row_i]

            shapes.append(
                RectShape(
                    cut_index=cut_idx,
                    width=config.geometry.cell_size,
                    height=config.geometry.cell_size,
                    xform=XForm.translate(cx, cy),
                )
            )

            if config.annotations.show_cell_text:
                line_half_gap = font.h_cell * 0.15
                y_line1 = cy - line_half_gap - font.h_cell / 2.0
                y_line2 = cy + line_half_gap + font.h_cell / 2.0
                margin = font.h_cell * 0.2
                half_cell = config.geometry.cell_size / 2.0
                y_line1 = max(cy - half_cell + margin + font.h_cell / 2.0, y_line1)
                y_line2 = min(cy + half_cell - margin - font.h_cell / 2.0, y_line2)

                add_text(fmt_val(x_val), cx, y_line1, font.h_cell, ah=HAlign.CENTER, av=VAlign.BOTTOM)
                add_text(fmt_val(y_val), cx, y_line2, font.h_cell, ah=HAlign.CENTER, av=VAlign.TOP)

    # Optional border
    if config.border.enabled and border_layer_index is not None:
        p = config.border.padding
        border_w = (x_grid_right - x_grid_left) + 2.0 * p
        border_h = (y_grid_bottom - y_grid_top) + 2.0 * p
        shapes.append(
            RectShape(
                cut_index=border_layer_index,
                width=border_w,
                height=border_h,
                xform=XForm.translate(x_grid_centre, y_grid_centre),
            )
        )

    notes = f"Generated by material_test.py  x={config.x.param}:{config.x.values}  y={config.y.param}:{config.y.values}"
    return LightBurnProject(cut_settings=cut_settings, shapes=shapes, notes=notes)


# ── CLI ────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "config", metavar="CONFIG.toml", help="Path to the TOML configuration file (see example_config.toml)"
    )
    p.add_argument(
        "-o",
        "--output",
        default=None,
        metavar="FILE",
        help="Output .lbrn2 file path (default: derived from config filename)",
    )
    args = p.parse_args(argv)

    with Path(args.config).open("rb") as f:
        data = tomllib.load(f)

    config = GridConfig.model_validate(data)

    output_path = args.output
    if output_path is None:
        output_path = str(Path(args.config).with_suffix(".lbrn2"))

    project = generate(config)

    with Path(output_path).open("w", encoding="utf-8") as f:
        f.write(project.to_xml_str())

    n_cells = len(config.x.values) * len(config.y.values)
    print(f"Written {output_path}  ({len(config.x.values)} cols x {len(config.y.values)} rows = {n_cells} cells)")


if __name__ == "__main__":
    main()
