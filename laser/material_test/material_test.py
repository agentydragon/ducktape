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
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

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
from laser.ruida.rd_writer import RdJob, RdLayer, RdRect

# ── Cut parameter enum ─────────────────────────────────────────────────────────


class CutParam(StrEnum):
    """Named laser cut parameters that can be scanned or held constant."""

    POWER_PCT = "power_pct"  # sets both min_power and max_power simultaneously
    POWER_MIN_PCT = "power_min_pct"
    POWER_MAX_PCT = "power_max_pct"
    SPEED_MM_S = "speed_mm_s"
    KERF_MM = "kerf_mm"
    Z_OFFSET_MM = "z_offset_mm"
    Z_PER_PASS_MM = "z_per_pass_mm"
    NUM_PASSES = "num_passes"


_PARAM: dict[CutParam, tuple[str, str, bool]] = {
    # (label, unit, abbreviate_in_subtitle) — when abbreviate_in_subtitle is
    # True, the subtitle omits the label and just prints "value unit".
    CutParam.POWER_PCT: ("PWR", "%", False),
    CutParam.POWER_MIN_PCT: ("PWR min", "%", False),
    CutParam.POWER_MAX_PCT: ("PWR max", "%", False),
    CutParam.SPEED_MM_S: ("Speed", "mm/s", True),
    CutParam.KERF_MM: ("Kerf", "mm", False),
    CutParam.Z_OFFSET_MM: ("Z", "mm", False),
    CutParam.Z_PER_PASS_MM: ("Z/pass", "mm", False),
    CutParam.NUM_PASSES: ("Passes", "", False),
}


def fmt_val(v: float) -> str:
    """Format a parameter value for human display (no trailing zeros)."""
    if v == int(v):
        return str(int(v))
    return f"{v:.4g}"


def _fmt_val_with_unit(param: CutParam, v: float) -> str:
    """Format a parameter value with its unit (e.g. '25%', '12 mm/s')."""
    _, unit, _ = _PARAM[param]
    formatted = fmt_val(v)
    if not unit:
        return formatted
    return f"{formatted}{unit}"


def _param_short_label(param: CutParam) -> str:
    """Short label for a parameter, used in the legend cell.

    - Abbreviable + has unit → just unit (e.g. "mm/s")
    - Not abbreviable + has unit → "Label unit" (e.g. "PWR%")
    - No unit → just label (e.g. "Passes")
    """
    label, unit, abbrev = _PARAM[param]
    if unit:
        if abbrev:
            return unit
        sep = "" if unit == "%" else " "
        return f"{label}{sep}{unit}"
    return label


# ── Pydantic config models ─────────────────────────────────────────────────────


class AxisConfig(BaseModel):
    """Configuration for one grid axis (X or Y)."""

    model_config = ConfigDict(extra="forbid")

    param: CutParam
    values: list[float]
    label: str | None = None  # None = auto-generate from param name + unit; "" = no label
    show_labels: bool = True


class CutConfig(BaseModel):
    """Laser cut parameters held constant across the entire grid.

    The x/y axis parameters override their respective entries here for each cell.

    Use 'power_pct' to set both power_min_pct and power_max_pct simultaneously.
    """

    model_config = ConfigDict(extra="forbid")

    power_pct: float | None = None  # shorthand: sets both power_min_pct and power_max_pct
    power_min_pct: float = 80.0
    power_max_pct: float = 80.0
    speed_mm_s: float = 100.0
    kerf_mm: float = 0.0
    z_offset_mm: float = 0.0
    z_per_pass_mm: float = 0.0  # Z step per pass (negative = deeper)
    num_passes: int = 1

    @model_validator(mode="after")
    def apply_power_shorthand(self) -> CutConfig:
        if self.power_pct is not None:
            self.power_min_pct = self.power_pct
            self.power_max_pct = self.power_pct
        return self

    def to_cut_setting(self, index: int, name: str) -> CutSetting:
        return CutSetting(
            index=index,
            name=name,
            min_power=self.power_min_pct,
            max_power=self.power_max_pct,
            speed=self.speed_mm_s,
            kerf=self.kerf_mm,
            z_offset=self.z_offset_mm,
            z_per_pass=self.z_per_pass_mm,
            num_passes=self.num_passes,
        )


class GeometryConfig(BaseModel):
    """Physical dimensions of the test grid cells."""

    model_config = ConfigDict(extra="forbid")

    cell_size_mm: float = 15.0  # square side length
    gap_mm: float = 8.0  # gap between adjacent cells
    subgrid_gap_mm: float = 20.0  # gap between sub-grids (3D/4D sweeps)


class CellContent(StrEnum):
    """What to show inside each grid cell."""

    NOTHING = "nothing"  # no in-cell text
    VALUES = "values"  # parameter values without units (e.g. "25")
    VALUES_WITH_UNITS = "values_with_units"  # parameter values with units (e.g. "25%")


class LabelsConfig(BaseModel):
    """Text label configuration for the grid."""

    model_config = ConfigDict(extra="forbid")

    cell_text: CellContent = CellContent.NOTHING
    cell_text_gap_mm: float = 0.3  # vertical gap between the two in-cell text lines
    show_legend: bool = False  # show a legend cell explaining in-cell text lines


class BorderConfig(BaseModel):
    """Optional border rectangle drawn around the entire grid."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    padding_mm: float = 3.0  # outside the grid cells
    power_pct: float = 10.0
    speed_mm_s: float = 200.0
    num_passes: int = 1


class TextLayerConfig(BaseModel):
    """Cut settings for the text label layer (layer 0)."""

    model_config = ConfigDict(extra="forbid")

    power_pct: float = 15.0
    speed_mm_s: float = 200.0


class FontConfig(BaseModel):
    """Font and text size configuration."""

    model_config = ConfigDict(extra="forbid")

    name: str = "Arial"
    h_title_mm: float = 10.0  # title text height
    h_subtitle_mm: float = 7.0
    h_label_mm: float = 6.0  # axis label
    h_value_mm: float = 5.0  # axis value labels
    h_cell_mm: float = 4.0  # in-cell parameter text


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
    cols: AxisConfig | None = None  # outer column axis (3D sweep)
    rows: AxisConfig | None = None  # outer row axis (4D sweep)
    cut: CutConfig = CutConfig()
    geometry: GeometryConfig = GeometryConfig()
    labels: LabelsConfig = LabelsConfig()
    border: BorderConfig = BorderConfig()
    text_layer: TextLayerConfig = TextLayerConfig()
    font: FontConfig = FontConfig()

    @model_validator(mode="after")
    def validate_axes(self) -> GridConfig:
        axes: list[tuple[str, CutParam]] = [("x", self.x.param), ("y", self.y.param)]
        if self.cols is not None:
            axes.append(("cols", self.cols.param))
        if self.rows is not None:
            axes.append(("rows", self.rows.param))
        for name, param in axes:
            if param in (CutParam.POWER_MIN_PCT, CutParam.POWER_MAX_PCT):
                raise ValueError(
                    f"{name}.param={param!r} is not allowed as an axis parameter; "
                    f"use 'power_pct' instead to vary min and max power together"
                )
        seen: dict[CutParam, str] = {}
        for name, param in axes:
            if param in seen:
                raise ValueError(f"{name}.param and {seen[param]}.param must be different; both are {param!r}")
            seen[param] = name
        return self


# ── Parameter helpers ──────────────────────────────────────────────────────────


def _apply_param(cut: CutSetting, param: CutParam, value: float) -> None:
    """Apply a named parameter value to a CutSetting."""
    if param == CutParam.POWER_PCT:
        cut.min_power = value
        cut.max_power = value
    elif param == CutParam.POWER_MIN_PCT:
        cut.min_power = value
    elif param == CutParam.POWER_MAX_PCT:
        cut.max_power = value
    elif param == CutParam.SPEED_MM_S:
        cut.speed = value
    elif param == CutParam.KERF_MM:
        cut.kerf = value
    elif param == CutParam.Z_OFFSET_MM:
        cut.z_offset = value
    elif param == CutParam.Z_PER_PASS_MM:
        cut.z_per_pass = value
    elif param == CutParam.NUM_PASSES:
        cut.num_passes = round(value)


def _get_param(cut: CutSetting, param: CutParam) -> float:
    """Read a named parameter value from a CutSetting."""
    if param in (CutParam.POWER_PCT, CutParam.POWER_MAX_PCT):
        return cut.max_power
    if param == CutParam.POWER_MIN_PCT:
        return cut.min_power
    if param == CutParam.SPEED_MM_S:
        return cut.speed
    if param == CutParam.KERF_MM:
        return cut.kerf
    if param == CutParam.Z_OFFSET_MM:
        return cut.z_offset
    if param == CutParam.Z_PER_PASS_MM:
        return cut.z_per_pass
    if param == CutParam.NUM_PASSES:
        return float(cut.num_passes)
    raise ValueError(f"Unknown param: {param!r}")  # unreachable with enum


def _auto_subtitle(config: GridConfig) -> str:
    """Build a subtitle listing the parameters constant across all cells."""
    varied: set[CutParam] = {config.x.param, config.y.param}
    if config.cols is not None:
        varied.add(config.cols.param)
    if config.rows is not None:
        varied.add(config.rows.param)
    if CutParam.POWER_PCT in varied:
        varied |= {CutParam.POWER_MIN_PCT, CutParam.POWER_MAX_PCT}
    if CutParam.POWER_MIN_PCT in varied or CutParam.POWER_MAX_PCT in varied:
        varied.add(CutParam.POWER_PCT)

    base = config.cut.to_cut_setting(0, "")
    parts: list[str] = []
    for param in [
        CutParam.Z_OFFSET_MM,
        CutParam.SPEED_MM_S,
        CutParam.KERF_MM,
        CutParam.Z_PER_PASS_MM,
        CutParam.NUM_PASSES,
        CutParam.POWER_PCT,
    ]:
        if param in varied:
            continue
        v = _get_param(base, param)
        if param == CutParam.NUM_PASSES and v == 1:
            continue
        label, unit, abbrev = _PARAM[param]
        if abbrev and unit:
            parts.append(f"{fmt_val(v)}{unit}")
        else:
            parts.append(f"{label}={fmt_val(v)}{unit}")
    return " ".join(parts)


def _full_subtitle(config: GridConfig) -> str:
    pieces: list[str] = []
    if config.subtitle:
        pieces.append(config.subtitle)
    if config.auto_subtitle:
        auto = _auto_subtitle(config)
        if auto:
            pieces.append(auto)
    return " ".join(pieces)


def _auto_label(param: CutParam) -> str:
    """Build an axis label from a CutParam (e.g. POWER_MAX_PCT → 'Power max [%]')."""
    label, unit, _abbrev = _PARAM[param]
    return f"{label} [{unit}]" if unit else label


def _cell_energy(config: GridConfig, outer_params: list[tuple[CutParam, float]], x_val: float, y_val: float) -> float:
    """Energy proxy for sort ordering: higher value = more fire risk.

    Computes max_power * num_passes / speed from the effective parameter values.
    """
    power = config.cut.power_max_pct
    speed = config.cut.speed_mm_s
    passes = config.cut.num_passes
    for param, val in [(config.x.param, x_val), (config.y.param, y_val), *outer_params]:
        if param in (CutParam.POWER_PCT, CutParam.POWER_MAX_PCT):
            power = val
        elif param == CutParam.SPEED_MM_S:
            speed = val
        elif param == CutParam.NUM_PASSES:
            passes = round(val)
    return power * passes / speed


# ── Layout constants ───────────────────────────────────────────────────────────

_SPACING = 3.0  # mm between text elements
_MARGIN_LEFT = 5.0  # mm left of the Y-axis label
_MARGIN_TOP = 5.0  # mm above the title
_TEXT_LAYER = 0  # layer index reserved for text labels


def _estimate_text_width(text: str, height: float) -> float:
    """Rough rendered text width estimate (approx 0.55 x height per char)."""
    return len(text) * height * 0.55


# ── Grid generation ────────────────────────────────────────────────────────────


def _make_text(
    text: str,
    x: float,
    y: float,
    height: float,
    font_name: str,
    ah: HAlign = HAlign.LEFT,
    av: VAlign = VAlign.TOP,
    *,
    rotate90ccw: bool = False,
) -> TextShape:
    xform = XForm.rotate90ccw(x, y) if rotate90ccw else XForm.translate(x, y)
    return TextShape(cut_index=_TEXT_LAYER, text=text, height=height, xform=xform, font=font_name, ah=ah, av=av)


def _y_label_width(config: GridConfig) -> float:
    """Horizontal space used by inner Y-axis labels (left of cell grid)."""
    if not config.y.show_labels:
        return 0.0
    return config.font.h_label_mm + _SPACING + config.font.h_value_mm + _SPACING


def _x_label_height(config: GridConfig) -> float:
    """Vertical space used by inner X-axis labels (below cell grid)."""
    if not config.x.show_labels:
        return 0.0
    x_label = config.x.label if config.x.label is not None else _auto_label(config.x.param)
    h = _SPACING + config.font.h_value_mm + _SPACING
    if x_label:
        h += config.font.h_label_mm
    return h


@dataclass
class _CellSpec:
    """A cell's parameter values and grid position indices (no absolute position).

    Used for energy-based sorting before assigning layer indices. Both
    generate() (LightBurn) and generate_rd() (Ruida) compute absolute
    positions from these grid indices using their own coordinate systems.
    """

    x_val: float
    y_val: float
    outer_params: list[tuple[CutParam, float]]
    outer_col: int  # index into config.cols.values (0 if no cols axis)
    outer_row: int  # index into config.rows.values (0 if no rows axis)
    inner_col: int  # index into config.x.values
    inner_row: int  # index into config.y.values


def _sorted_cell_specs(config: GridConfig) -> list[_CellSpec]:
    """Collect all cell parameter combinations and sort by ascending energy."""
    n_outer_cols = len(config.cols.values) if config.cols is not None else 1
    n_outer_rows = len(config.rows.values) if config.rows is not None else 1

    specs: list[_CellSpec] = []
    for or_i in range(n_outer_rows):
        for oc_j in range(n_outer_cols):
            outer_params: list[tuple[CutParam, float]] = []
            if config.cols is not None:
                outer_params.append((config.cols.param, config.cols.values[oc_j]))
            if config.rows is not None:
                outer_params.append((config.rows.param, config.rows.values[or_i]))

            for row_i, y_val in enumerate(config.y.values):
                for col_j, x_val in enumerate(config.x.values):
                    specs.append(
                        _CellSpec(
                            x_val=x_val,
                            y_val=y_val,
                            outer_params=outer_params,
                            outer_col=oc_j,
                            outer_row=or_i,
                            inner_col=col_j,
                            inner_row=row_i,
                        )
                    )

    specs.sort(key=lambda c: _cell_energy(config, c.outer_params, c.x_val, c.y_val))
    return specs


def _cell_cut_setting(config: GridConfig, spec: _CellSpec, index: int, name: str) -> CutSetting:
    """Create a CutSetting for a grid cell, applying the cell's parameter values."""
    cut = config.cut.to_cut_setting(index, name)
    _apply_param(cut, config.x.param, spec.x_val)
    _apply_param(cut, config.y.param, spec.y_val)
    for param, value in spec.outer_params:
        _apply_param(cut, param, value)
    return cut


def _generate_subgrid_labels(
    config: GridConfig,
    cell_grid_left: float,
    cell_grid_top: float,
    *,
    emit_x_labels: bool = True,
    emit_y_labels: bool = True,
) -> list[AnyShape]:
    """Generate text labels for a single 2D sub-grid.

    Places labels around cells starting at (cell_grid_left, cell_grid_top)
    in LightBurn's Y-up coordinate system.

    Cell rectangles are NOT created here — they are generated after global
    energy-based sorting in generate().
    """
    n_cols = len(config.x.values)
    n_rows = len(config.y.values)
    stride = config.geometry.cell_size_mm + config.geometry.gap_mm
    font = config.font

    shapes: list[AnyShape] = []

    # Cell centre positions (Y-up: top is larger Y)
    x_col = [cell_grid_left + config.geometry.cell_size_mm / 2.0 + j * stride for j in range(n_cols)]
    y_row = [cell_grid_top - config.geometry.cell_size_mm / 2.0 - i * stride for i in range(n_rows)]

    cell_grid_right = cell_grid_left + n_cols * stride - config.geometry.gap_mm
    cell_grid_bottom = cell_grid_top - (n_rows * stride - config.geometry.gap_mm)
    cell_grid_x_centre = (cell_grid_left + cell_grid_right) / 2.0
    cell_grid_y_centre = (cell_grid_top + cell_grid_bottom) / 2.0

    # In-cell text labels
    for row_i, y_val in enumerate(config.y.values):
        for col_j, x_val in enumerate(config.x.values):
            cx = x_col[col_j]
            cy = y_row[row_i]

            if config.labels.cell_text != CellContent.NOTHING:
                _cell_fmt = (
                    _fmt_val_with_unit
                    if config.labels.cell_text == CellContent.VALUES_WITH_UNITS
                    else lambda _p, v: fmt_val(v)
                )
                half_gap = config.labels.cell_text_gap_mm / 2.0
                y_line1 = cy + half_gap + font.h_cell_mm / 2.0
                y_line2 = cy - half_gap - font.h_cell_mm / 2.0
                margin = font.h_cell_mm * 0.2
                half_cell = config.geometry.cell_size_mm / 2.0
                y_line1 = min(cy + half_cell - margin - font.h_cell_mm / 2.0, y_line1)
                y_line2 = max(cy - half_cell + margin + font.h_cell_mm / 2.0, y_line2)

                shapes.append(
                    _make_text(
                        _cell_fmt(config.x.param, x_val),
                        cx,
                        y_line1,
                        font.h_cell_mm,
                        font.name,
                        ah=HAlign.CENTER,
                        av=VAlign.BOTTOM,
                    )
                )
                shapes.append(
                    _make_text(
                        _cell_fmt(config.y.param, y_val),
                        cx,
                        y_line2,
                        font.h_cell_mm,
                        font.name,
                        ah=HAlign.CENTER,
                        av=VAlign.TOP,
                    )
                )

    # Inner X-axis labels (below grid)
    x_label = config.x.label if config.x.label is not None else _auto_label(config.x.param)
    if config.x.show_labels and emit_x_labels:
        y_below = cell_grid_bottom - _SPACING
        for col_j, x_val in enumerate(config.x.values):
            shapes.append(
                _make_text(
                    fmt_val(x_val), x_col[col_j], y_below, font.h_value_mm, font.name, ah=HAlign.CENTER, av=VAlign.TOP
                )
            )
        y_below -= font.h_value_mm + _SPACING

        if x_label:
            shapes.append(
                _make_text(x_label, cell_grid_x_centre, y_below, font.h_label_mm, font.name, ah=HAlign.CENTER)
            )

    # Inner Y-axis labels (left of grid, rotated 90° CCW)
    y_label = config.y.label if config.y.label is not None else _auto_label(config.y.param)
    if config.y.show_labels and emit_y_labels:
        x_y_val_cx = cell_grid_left - _SPACING - config.font.h_value_mm / 2.0
        x_y_label_cx = cell_grid_left - _SPACING - config.font.h_value_mm - _SPACING - config.font.h_label_mm / 2.0

        for row_i, y_val in enumerate(config.y.values):
            shapes.append(
                _make_text(
                    fmt_val(y_val),
                    x_y_val_cx,
                    y_row[row_i],
                    font.h_value_mm,
                    font.name,
                    ah=HAlign.CENTER,
                    av=VAlign.CENTER,
                    rotate90ccw=True,
                )
            )

        if y_label:
            shapes.append(
                _make_text(
                    y_label,
                    x_y_label_cx,
                    cell_grid_y_centre,
                    font.h_label_mm,
                    font.name,
                    ah=HAlign.CENTER,
                    av=VAlign.CENTER,
                    rotate90ccw=True,
                )
            )

    return shapes


def generate(config: GridConfig) -> LightBurnProject:
    """Build a LightBurnProject from the grid configuration.

    Supports 2D (x x y), 3D (+ cols or rows), and 4D (+ cols + rows) sweeps.
    For 3D/4D, generates a grid of sub-grids where each sub-grid is a complete
    2D grid with its own inner axis labels.
    """
    font = config.font

    cut_settings: list[CutSetting] = []
    shapes: list[AnyShape] = []

    # Layer 0: text labels
    cut_settings.append(
        CutSetting(
            index=_TEXT_LAYER,
            name="Text",
            mode=CutMode.CUT,
            min_power=config.text_layer.power_pct,
            max_power=config.text_layer.power_pct,
            speed=config.text_layer.speed_mm_s,
        )
    )

    # Outer grid dimensions
    n_outer_cols = len(config.cols.values) if config.cols is not None else 1
    n_outer_rows = len(config.rows.values) if config.rows is not None else 1

    # Sub-grid cell grid dimensions
    inner_stride = config.geometry.cell_size_mm + config.geometry.gap_mm
    cell_grid_w = len(config.x.values) * inner_stride - config.geometry.gap_mm
    cell_grid_h = len(config.y.values) * inner_stride - config.geometry.gap_mm
    y_aw = _y_label_width(config)
    x_ah = _x_label_height(config)
    subgrid_gap = config.geometry.subgrid_gap_mm

    # ── Horizontal layout ─────────────────────────────────────────────────────
    x_cursor = _MARGIN_LEFT

    # Outer rows axis labels (rotated, left side)
    has_outer_rows = config.rows is not None
    outer_rows_label = ""
    x_outer_rows_label_cx = x_cursor
    x_outer_rows_val_cx = x_cursor
    if has_outer_rows:
        assert config.rows is not None
        outer_rows_label = config.rows.label if config.rows.label is not None else _auto_label(config.rows.param)
        if config.rows.show_labels:
            if outer_rows_label:
                x_outer_rows_label_cx = x_cursor + font.h_label_mm / 2.0
                x_cursor += font.h_label_mm + _SPACING
            x_outer_rows_val_cx = x_cursor + font.h_value_mm / 2.0
            x_cursor += font.h_value_mm + _SPACING

    # Sub-grid positions — Y labels only on the first column, so y_aw
    # space is reserved once (not per sub-grid).
    cell_grid_lefts = [x_cursor + y_aw + oc * (cell_grid_w + subgrid_gap) for oc in range(n_outer_cols)]
    total_right = cell_grid_lefts[-1] + cell_grid_w

    # Centre X for title/subtitle: centred on the cell grids across all sub-grids
    all_cell_left = cell_grid_lefts[0]
    all_cell_right = cell_grid_lefts[-1] + cell_grid_w
    centre_x = (all_cell_left + all_cell_right) / 2.0

    # ── Vertical layout ───────────────────────────────────────────────────────
    subtitle_text = _full_subtitle(config)

    # Outer cols axis labels (above sub-grids)
    has_outer_cols = config.cols is not None
    outer_cols_label = ""
    outer_cols_label_h = 0.0
    if has_outer_cols:
        assert config.cols is not None
        outer_cols_label = config.cols.label if config.cols.label is not None else _auto_label(config.cols.param)
        if config.cols.show_labels:
            outer_cols_label_h += font.h_value_mm + _SPACING
            if outer_cols_label:
                outer_cols_label_h += font.h_label_mm + _SPACING

    # X labels only on the last row, so x_ah space is reserved once.
    total_subgrids_h = n_outer_rows * cell_grid_h + max(0, n_outer_rows - 1) * subgrid_gap + x_ah

    v_total = 0.0
    if config.title:
        v_total += font.h_title_mm + _SPACING
    if subtitle_text:
        v_total += font.h_subtitle_mm + _SPACING
    v_total += outer_cols_label_h
    v_total += total_subgrids_h

    # y starts at the top (large Y) and decreases as we place elements downward.
    y = v_total + _MARGIN_TOP

    content_left = 0.0
    content_right = total_right
    content_top = y

    # Title
    if config.title:
        shapes.append(_make_text(config.title, centre_x, y, font.h_title_mm, font.name, ah=HAlign.CENTER))
        title_half_w = _estimate_text_width(config.title, font.h_title_mm) / 2.0
        content_right = max(content_right, centre_x + title_half_w)
        y -= font.h_title_mm + _SPACING

    # Subtitle
    if subtitle_text:
        shapes.append(_make_text(subtitle_text, centre_x, y, font.h_subtitle_mm, font.name, ah=HAlign.CENTER))
        sub_half_w = _estimate_text_width(subtitle_text, font.h_subtitle_mm) / 2.0
        content_right = max(content_right, centre_x + sub_half_w)
        y -= font.h_subtitle_mm + _SPACING

    # Outer cols labels (values per column, then label)
    if has_outer_cols and config.cols is not None and config.cols.show_labels:
        for oc, col_val in enumerate(config.cols.values):
            sg_cell_centre_x = cell_grid_lefts[oc] + cell_grid_w / 2.0
            shapes.append(
                _make_text(
                    fmt_val(col_val), sg_cell_centre_x, y, font.h_value_mm, font.name, ah=HAlign.CENTER, av=VAlign.TOP
                )
            )
        y -= font.h_value_mm + _SPACING

        if outer_cols_label:
            shapes.append(_make_text(outer_cols_label, centre_x, y, font.h_label_mm, font.name, ah=HAlign.CENTER))
            y -= font.h_label_mm + _SPACING

    # Generate sub-grid text labels
    subgrid_cell_grid_tops: list[float] = []

    for or_i in range(n_outer_rows):
        cell_grid_top_y = y - or_i * (cell_grid_h + subgrid_gap)
        subgrid_cell_grid_tops.append(cell_grid_top_y)

        for oc_j in range(n_outer_cols):
            shapes.extend(
                _generate_subgrid_labels(
                    config=config,
                    cell_grid_left=cell_grid_lefts[oc_j],
                    cell_grid_top=cell_grid_top_y,
                    emit_y_labels=oc_j == 0,
                    emit_x_labels=or_i == n_outer_rows - 1,
                )
            )

    # Legend cell in the bottom-left corner (Y-label area x X-label area)
    if config.labels.show_legend and config.labels.cell_text != CellContent.NOTHING:
        last_subgrid_bottom = subgrid_cell_grid_tops[-1] - cell_grid_h
        # Use label area if present, otherwise position just outside the cell grid
        effective_y_aw = y_aw if y_aw > 0 else config.geometry.cell_size_mm + _SPACING
        effective_x_ah = x_ah if x_ah > 0 else config.geometry.cell_size_mm + _SPACING
        corner_top = last_subgrid_bottom
        corner_bottom = corner_top - effective_x_ah
        legend_cx = cell_grid_lefts[0] - effective_y_aw / 2.0
        legend_cy = (corner_top + corner_bottom) / 2.0

        shapes.append(
            RectShape(
                cut_index=_TEXT_LAYER,
                width=config.geometry.cell_size_mm,
                height=config.geometry.cell_size_mm,
                xform=XForm.translate(legend_cx, legend_cy),
            )
        )

        half_gap = config.labels.cell_text_gap_mm / 2.0
        y_line1 = legend_cy + half_gap + font.h_cell_mm / 2.0
        y_line2 = legend_cy - half_gap - font.h_cell_mm / 2.0
        margin = font.h_cell_mm * 0.2
        half_cell = config.geometry.cell_size_mm / 2.0
        y_line1 = min(legend_cy + half_cell - margin - font.h_cell_mm / 2.0, y_line1)
        y_line2 = max(legend_cy - half_cell + margin + font.h_cell_mm / 2.0, y_line2)

        shapes.append(
            _make_text(
                _param_short_label(config.x.param),
                legend_cx,
                y_line1,
                font.h_cell_mm,
                font.name,
                ah=HAlign.CENTER,
                av=VAlign.BOTTOM,
            )
        )
        shapes.append(
            _make_text(
                _param_short_label(config.y.param),
                legend_cx,
                y_line2,
                font.h_cell_mm,
                font.name,
                ah=HAlign.CENTER,
                av=VAlign.TOP,
            )
        )

    # Create cell layers sorted by ascending energy.
    # Cell positions use LightBurn's Y-up coordinate system.
    sorted_cells = _sorted_cell_specs(config)
    next_layer_index = 1
    for spec in sorted_cells:
        cut = _cell_cut_setting(config, spec, next_layer_index, f"C{next_layer_index:02d}")
        cut_settings.append(cut)

        sg_top = subgrid_cell_grid_tops[spec.outer_row]
        cx = cell_grid_lefts[spec.outer_col] + config.geometry.cell_size_mm / 2.0 + spec.inner_col * inner_stride
        cy = sg_top - config.geometry.cell_size_mm / 2.0 - spec.inner_row * inner_stride
        shapes.append(
            RectShape(
                cut_index=next_layer_index,
                width=config.geometry.cell_size_mm,
                height=config.geometry.cell_size_mm,
                xform=XForm.translate(cx, cy),
            )
        )
        next_layer_index += 1

    # Outer rows labels (left side, rotated 90° CCW)
    if has_outer_rows and config.rows is not None and config.rows.show_labels:
        for or_i, row_val in enumerate(config.rows.values):
            sg_cell_centre_y = subgrid_cell_grid_tops[or_i] - cell_grid_h / 2.0
            shapes.append(
                _make_text(
                    fmt_val(row_val),
                    x_outer_rows_val_cx,
                    sg_cell_centre_y,
                    font.h_value_mm,
                    font.name,
                    ah=HAlign.CENTER,
                    av=VAlign.CENTER,
                    rotate90ccw=True,
                )
            )

        if outer_rows_label:
            all_sg_top = subgrid_cell_grid_tops[0]
            all_sg_bottom = subgrid_cell_grid_tops[-1] - cell_grid_h
            all_sg_centre_y = (all_sg_top + all_sg_bottom) / 2.0
            shapes.append(
                _make_text(
                    outer_rows_label,
                    x_outer_rows_label_cx,
                    all_sg_centre_y,
                    font.h_label_mm,
                    font.name,
                    ah=HAlign.CENTER,
                    av=VAlign.CENTER,
                    rotate90ccw=True,
                )
            )

    # Content bottom
    content_bottom = subgrid_cell_grid_tops[-1] - cell_grid_h - x_ah

    # Optional border
    if config.border.enabled:
        cut_settings.append(
            CutSetting(
                index=next_layer_index,
                name="Border",
                mode=CutMode.CUT,
                min_power=config.border.power_pct,
                max_power=config.border.power_pct,
                speed=config.border.speed_mm_s,
                num_passes=config.border.num_passes,
            )
        )
        p = config.border.padding_mm
        border_w = (content_right - content_left) + 2.0 * p
        border_h = (content_top - content_bottom) + 2.0 * p
        border_cx = (content_left + content_right) / 2.0
        border_cy = (content_top + content_bottom) / 2.0
        shapes.append(
            RectShape(
                cut_index=next_layer_index, width=border_w, height=border_h, xform=XForm.translate(border_cx, border_cy)
            )
        )
        next_layer_index += 1

    notes_parts = [
        "Generated by material_test.py",
        f"x={config.x.param}:{config.x.values}",
        f"y={config.y.param}:{config.y.values}",
    ]
    if config.cols is not None:
        notes_parts.append(f"cols={config.cols.param}:{config.cols.values}")
    if config.rows is not None:
        notes_parts.append(f"rows={config.rows.param}:{config.rows.values}")
    return LightBurnProject(cut_settings=cut_settings, shapes=shapes, notes="  ".join(notes_parts))


def generate_rd(config: GridConfig) -> RdJob:
    """Build an RdJob from the grid configuration (no text labels, cells + border only).

    Uses the same energy-based sort ordering as generate(). Coordinates are in
    Ruida's Y-down system (Y increases downward, origin at top-left).
    """
    n_outer_cols = len(config.cols.values) if config.cols is not None else 1
    n_outer_rows = len(config.rows.values) if config.rows is not None else 1
    inner_stride = config.geometry.cell_size_mm + config.geometry.gap_mm
    cell_grid_w = len(config.x.values) * inner_stride - config.geometry.gap_mm
    cell_grid_h = len(config.y.values) * inner_stride - config.geometry.gap_mm
    subgrid_gap = config.geometry.subgrid_gap_mm

    # Sub-grid positions (Y-down: top-left origin)
    cell_grid_lefts = [oc * (cell_grid_w + subgrid_gap) for oc in range(n_outer_cols)]
    cell_grid_tops = [or_i * (cell_grid_h + subgrid_gap) for or_i in range(n_outer_rows)]

    sorted_cells = _sorted_cell_specs(config)
    layers: list[RdLayer] = []
    rects: list[RdRect] = []

    for i, spec in enumerate(sorted_cells):
        cut = _cell_cut_setting(config, spec, i, "")
        layers.append(
            RdLayer(
                index=i,
                min_power_pct=cut.min_power,
                max_power_pct=cut.max_power,
                speed_mm_s=cut.speed,
                num_passes=cut.num_passes,
                z_offset_mm=cut.z_offset,
                z_per_pass_mm=cut.z_per_pass,
            )
        )
        # Cell position in Y-down coords
        x = cell_grid_lefts[spec.outer_col] + spec.inner_col * inner_stride
        y_rd = cell_grid_tops[spec.outer_row] + spec.inner_row * inner_stride
        rects.append(
            RdRect(
                layer_index=i,
                x_mm=x,
                y_mm=y_rd,
                width_mm=config.geometry.cell_size_mm,
                height_mm=config.geometry.cell_size_mm,
            )
        )

    # Optional border (last layer)
    if config.border.enabled:
        border_idx = len(layers)
        all_x_min = min(r.x_mm for r in rects)
        all_y_min = min(r.y_mm for r in rects)
        all_x_max = max(r.x_mm + r.width_mm for r in rects)
        all_y_max = max(r.y_mm + r.height_mm for r in rects)
        p = config.border.padding_mm
        layers.append(
            RdLayer(
                index=border_idx,
                min_power_pct=config.border.power_pct,
                max_power_pct=config.border.power_pct,
                speed_mm_s=config.border.speed_mm_s,
                num_passes=config.border.num_passes,
            )
        )
        rects.append(
            RdRect(
                layer_index=border_idx,
                x_mm=all_x_min - p,
                y_mm=all_y_min - p,
                width_mm=(all_x_max - all_x_min) + 2 * p,
                height_mm=(all_y_max - all_y_min) + 2 * p,
            )
        )

    return RdJob(layers=layers, rects=rects)


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
        help="Output file path (default: derived from config filename + format)",
    )
    p.add_argument(
        "--format",
        choices=["lbrn2", "rd"],
        default="lbrn2",
        dest="fmt",
        help="Output format: lbrn2 (LightBurn XML, max 30 layers) or rd (Ruida binary, up to 128 layers)",
    )
    args = p.parse_args(argv)

    with Path(args.config).open("rb") as f:
        data = tomllib.load(f)

    config = GridConfig.model_validate(data)

    output_path = args.output
    if output_path is None:
        output_path = str(Path(args.config).with_suffix(f".{args.fmt}"))

    if args.fmt == "rd":
        rd_job = generate_rd(config)
        with Path(output_path).open("wb") as fb:
            fb.write(rd_job.to_bytes())
    else:
        project = generate(config)
        with Path(output_path).open("w", encoding="utf-8") as f:
            f.write(project.to_xml_str())

    inner_cells = len(config.x.values) * len(config.y.values)
    n_outer_cols = len(config.cols.values) if config.cols else 1
    n_outer_rows = len(config.rows.values) if config.rows else 1
    total_cells = inner_cells * n_outer_cols * n_outer_rows
    inner_desc = f"{len(config.x.values)}x{len(config.y.values)}"
    if config.cols or config.rows:
        outer_parts = []
        if config.cols:
            outer_parts.append(f"{n_outer_cols} cols")
        if config.rows:
            outer_parts.append(f"{n_outer_rows} rows")
        print(f"Written {output_path}  ({inner_desc} inner x {' x '.join(outer_parts)} = {total_cells} cells)")
    else:
        print(f"Written {output_path}  ({inner_desc} = {total_cells} cells)")


if __name__ == "__main__":
    main()
