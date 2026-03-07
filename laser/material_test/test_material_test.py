"""Tests for the material test grid generator."""

from __future__ import annotations

import math
import textwrap
import tomllib
from xml.etree import ElementTree as ET

import pytest
import pytest_bazel

from laser.lightburn.lbrn2_writer import CutSetting, HAlign, LightBurnProject, RectShape, TextShape, VAlign, XForm
from laser.material_test.material_test import (
    AxisConfig,
    BorderConfig,
    CellContent,
    CutConfig,
    CutParam,
    GeometryConfig,
    GridConfig,
    LabelsConfig,
    _full_subtitle,
    _param_short_label,
    fmt_val,
    generate,
)
from util.bazel import runfiles

# ── fmt_val ────────────────────────────────────────────────────────────────────


def test_fmt_val_integer():
    assert fmt_val(15.0) == "15"


def test_fmt_val_negative():
    assert fmt_val(-0.5) == "-0.5"


def test_fmt_val_zero():
    assert fmt_val(0.0) == "0"


def test_fmt_val_trailing_zero():
    assert fmt_val(13.5) == "13.5"


# ── XForm ──────────────────────────────────────────────────────────────────────


def test_xform_translate():
    xf = XForm.translate(10.5, 20.0)
    assert xf.to_str() == "1 0 0 1 10.5 20"


def test_xform_rotate90ccw():
    xf = XForm.rotate90ccw(50.0, 100.0)
    assert xf.to_str() == "0 1 -1 0 50 100"


def test_xform_rotate_45():
    xf = XForm.rotate(45.0)
    assert abs(xf.a - math.cos(math.radians(45))) < 1e-9
    assert abs(xf.b - math.sin(math.radians(45))) < 1e-9


# ── CutSetting ─────────────────────────────────────────────────────────────────


def test_cut_setting_to_element():
    cs = CutSetting(index=1, name="C01", min_power=20, max_power=80, speed=15)
    el = cs.to_element()
    assert el.tag == "CutSetting"
    assert el.attrib["type"] == "Cut"

    def val(name: str) -> str:
        child = el.find(name)
        assert child is not None, f"Missing child element <{name}>"
        return child.attrib["Value"]

    assert val("index") == "1"
    assert val("name") == "C01"
    assert val("minPower") == "20"
    assert val("maxPower") == "80"
    assert val("speed") == "15"


def test_cut_setting_z_params():
    cs = CutSetting(index=0, name="X", z_offset=-1.5, z_per_pass=-0.5, num_passes=3)
    el = cs.to_element()
    z_offset_el = el.find("zOffset")
    assert z_offset_el is not None
    assert z_offset_el.attrib["Value"] == "-1.5"
    z_per_pass_el = el.find("zPerPass")
    assert z_per_pass_el is not None
    assert z_per_pass_el.attrib["Value"] == "-0.5"
    num_passes_el = el.find("numPasses")
    assert num_passes_el is not None
    assert num_passes_el.attrib["Value"] == "3"


# ── RectShape / TextShape ──────────────────────────────────────────────────────


def test_rect_shape_element():
    rect = RectShape(cut_index=2, width=15.0, height=15.0, xform=XForm.translate(50.0, 30.0))
    el = rect.to_element()
    assert el.tag == "Shape"
    assert el.attrib["Type"] == "Rect"
    assert el.attrib["W"] == "15"
    assert el.attrib["CutIndex"] == "2"
    xform_el = el.find("XForm")
    assert xform_el is not None
    assert xform_el.text == "1 0 0 1 50 30"


def test_text_shape_element():
    txt = TextShape(
        cut_index=0, text="Hello", height=8.0, xform=XForm.translate(10.0, 5.0), ah=HAlign.CENTER, av=VAlign.CENTER
    )
    el = txt.to_element()
    assert el.attrib["Str"] == "Hello"
    assert el.attrib["Ah"] == "1"
    assert el.attrib["Av"] == "1"


def test_text_shape_xml_escaping():
    txt = TextShape(cut_index=0, text='A & B < C > "D"', height=5.0, xform=XForm.translate(0, 0))
    el = txt.to_element()
    # ElementTree stores the raw string; escaping happens at serialisation
    assert el.attrib["Str"] == 'A & B < C > "D"'
    # Confirm it round-trips through XML serialisation without error
    xml_str = ET.tostring(el, encoding="unicode")
    assert "&amp;" in xml_str
    assert "&lt;" in xml_str


def test_text_shape_rotated():
    txt = TextShape(
        cut_index=0, text="Y label", height=6.0, xform=XForm.rotate90ccw(10.0, 80.0), ah=HAlign.CENTER, av=VAlign.CENTER
    )
    el = txt.to_element()
    xform_el = el.find("XForm")
    assert xform_el is not None
    assert xform_el.text == "0 1 -1 0 10 80"


# ── LightBurnProject ──────────────────────────────────────────────────────────


def test_project_xml_valid():
    project = LightBurnProject(
        cut_settings=[CutSetting(index=0, name="Text")],
        shapes=[RectShape(cut_index=0, width=10, height=10, xform=XForm.translate(5, 5))],
        notes="test",
    )
    xml_str = project.to_xml_str()
    assert xml_str.startswith("<?xml")
    # Must parse without error
    root = ET.fromstring(xml_str.split("\n", 1)[1])
    assert root.tag == "LightBurnProject"
    assert root.attrib["FormatVersion"] == "1"
    assert root.find("CutSetting") is not None
    assert root.find("Shape") is not None
    assert root.find("Notes") is not None


# ── GridConfig (Pydantic) ──────────────────────────────────────────────────────


def _minimal_config(**kwargs) -> GridConfig:
    base = {
        "x": AxisConfig(param=CutParam.POWER_PCT, values=[10, 20, 30]),
        "y": AxisConfig(param=CutParam.SPEED_MM_S, values=[50, 100]),
    }
    base.update(kwargs)
    return GridConfig(**base)


def test_grid_config_minimal():
    cfg = _minimal_config()
    assert cfg.x.param == CutParam.POWER_PCT
    assert cfg.y.values == [50, 100]


def test_grid_config_rejects_same_axes():
    with pytest.raises(Exception, match="must be different"):
        GridConfig(
            x=AxisConfig(param=CutParam.SPEED_MM_S, values=[10, 20]),
            y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50, 100]),
        )


def test_grid_config_rejects_power_min_pct_axis():
    with pytest.raises(Exception, match="use 'power_pct'"):
        GridConfig(
            x=AxisConfig(param=CutParam.POWER_MIN_PCT, values=[10, 20]),
            y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50, 100]),
        )


def test_grid_config_rejects_power_max_pct_axis():
    with pytest.raises(Exception, match="use 'power_pct'"):
        GridConfig(
            x=AxisConfig(param=CutParam.POWER_MAX_PCT, values=[10, 20]),
            y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50, 100]),
        )


def test_cut_config_power_shorthand():
    cc = CutConfig(power_pct=75)
    assert cc.power_min_pct == 75
    assert cc.power_max_pct == 75


def test_cut_config_power_shorthand_does_not_override_explicit():
    # power_max_pct set explicitly should still be overridden by power_pct shorthand
    # (model_validator runs after all fields are set)
    cc = CutConfig(power_pct=60, power_max_pct=80)
    assert cc.power_min_pct == 60
    assert cc.power_max_pct == 60


def test_grid_config_from_toml():
    toml_str = textwrap.dedent("""\
        title = "Test"

        [x]
        param = "power_pct"
        values = [10.0, 20.0]
        label = "Power [%]"

        [y]
        param = "speed_mm_s"
        values = [50.0, 100.0]
        label = "Speed [mm/s]"

        [cut]
        speed_mm_s = 15
        z_offset_mm = -0.1
    """)
    data = tomllib.loads(toml_str)
    cfg = GridConfig.model_validate(data)
    assert cfg.title == "Test"
    assert cfg.x.param == CutParam.POWER_PCT
    assert cfg.cut.z_offset_mm == -0.1


# ── generate() ────────────────────────────────────────────────────────────────


def _make_project(
    x_values: list[float] | None = None, y_values: list[float] | None = None, **kwargs
) -> LightBurnProject:
    resolved_x = x_values if x_values is not None else [10.0, 20.0, 30.0]
    resolved_y = y_values if y_values is not None else [50.0, 100.0]
    cfg = GridConfig(
        title="Test grid",
        x=AxisConfig(param=CutParam.POWER_PCT, values=resolved_x, label="Power [%]"),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=resolved_y, label="Speed [mm/s]"),
        **kwargs,
    )
    return generate(cfg)


def test_generate_layer_count():
    project = _make_project()
    n_cells = 3 * 2
    # Layer 0 (text) + n_cells cell layers
    assert len(project.cut_settings) == 1 + n_cells


def test_generate_rect_count():
    project = _make_project()
    rects = [s for s in project.shapes if isinstance(s, RectShape)]
    assert len(rects) == 3 * 2


def test_generate_border_adds_layer_and_rect():
    project = _make_project(border=BorderConfig(enabled=True, power_pct=5, speed_mm_s=100))
    # border layer added
    assert any(cs.name == "Border" for cs in project.cut_settings)
    # border rect added (one extra rect)
    rects = [s for s in project.shapes if isinstance(s, RectShape)]
    assert len(rects) == 3 * 2 + 1


def test_generate_cell_cut_settings_have_correct_params():
    """Each cell layer should have x_param and y_param set correctly.

    Cells are sorted by ascending energy (max_power * num_passes / speed).
    x=power_pct [10, 20], y=speed_mm_s [50, 100]:
      (r0,c0) 10/50=0.2, (r0,c1) 20/50=0.4, (r1,c0) 10/100=0.1, (r1,c1) 20/100=0.2
    Sorted: 0.1 → 0.2 → 0.2 → 0.4
    """
    x_vals = [10.0, 20.0]
    y_vals = [50.0, 100.0]
    project = _make_project(x_values=x_vals, y_values=y_vals)
    cell_layers = project.cut_settings[1:]  # skip text layer
    assert len(cell_layers) == 4
    # Lowest energy: power=10, speed=100 (energy=0.1)
    assert cell_layers[0].max_power == 10.0
    assert cell_layers[0].speed == 100.0
    # power=10, speed=50 (energy=0.2)
    assert cell_layers[1].max_power == 10.0
    assert cell_layers[1].speed == 50.0
    # power=20, speed=100 (energy=0.2)
    assert cell_layers[2].max_power == 20.0
    assert cell_layers[2].speed == 100.0
    # Highest energy: power=20, speed=50 (energy=0.4)
    assert cell_layers[3].max_power == 20.0
    assert cell_layers[3].speed == 50.0


def test_cells_sorted_by_ascending_energy():
    """Cell layers must be in ascending energy order (max_power * num_passes / speed)."""
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0, 50.0, 90.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[25.0, 100.0, 200.0]),
    )
    project = generate(cfg)
    cell_layers = project.cut_settings[1:]  # skip text layer
    energies = [cs.max_power * cs.num_passes / cs.speed for cs in cell_layers]
    assert energies == sorted(energies)


def test_cells_sorted_by_energy_across_subgrids():
    """Energy sorting is global across sub-grids, not per-sub-grid."""
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0, 50.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[100.0]),
        cols=AxisConfig(param=CutParam.NUM_PASSES, values=[1, 5]),
    )
    project = generate(cfg)
    cell_layers = project.cut_settings[1:]
    energies = [cs.max_power * cs.num_passes / cs.speed for cs in cell_layers]
    assert energies == sorted(energies)
    # Verify interleaving: 1-pass cells (energy 0.1, 0.5) should come before
    # 5-pass 50% power (energy 2.5), not grouped by sub-grid.
    assert cell_layers[0].num_passes == 1
    assert cell_layers[0].max_power == 10.0
    assert cell_layers[1].num_passes == 1
    assert cell_layers[1].max_power == 50.0
    assert cell_layers[2].num_passes == 5
    assert cell_layers[2].max_power == 10.0
    assert cell_layers[3].num_passes == 5
    assert cell_layers[3].max_power == 50.0


def test_generate_xml_round_trips():
    project = _make_project()
    xml_str = project.to_xml_str()
    root = ET.fromstring(xml_str.split("\n", 1)[1])
    assert root.tag == "LightBurnProject"


def test_generate_with_cell_text():
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0, 20.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0]),
        labels=LabelsConfig(cell_text=CellContent.VALUES_WITH_UNITS),
    )
    project = generate(cfg)
    texts = [s for s in project.shapes if isinstance(s, TextShape)]
    # Should have in-cell texts (2 per cell x 2 cells = 4) plus axis labels
    assert len(texts) >= 4


def test_generate_no_labels():
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0, 20.0], show_labels=False),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0], show_labels=False),
        auto_subtitle=False,  # suppress auto-generated subtitle too
    )
    project = generate(cfg)
    texts = [s for s in project.shapes if isinstance(s, TextShape)]
    assert texts == []


def test_generate_auto_subtitle_omits_varied_params():
    """Auto-subtitle should not mention x or y axis parameters."""
    cfg = GridConfig(
        title="T",
        auto_subtitle=True,
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0, 20.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0, 100.0]),
        cut=CutConfig(z_offset_mm=-0.1, kerf_mm=0.05),
    )
    sub = _full_subtitle(cfg)
    assert "Power" not in sub
    assert "Speed" not in sub
    assert "Z=" in sub or "kerf" in sub.lower() or "Kerf" in sub


def test_example_config_parses():
    """example_config.toml must parse without error and produce a valid GridConfig."""
    path = runfiles.get_required_path("_main/laser/material_test/example_config.toml")
    with path.open("rb") as f:
        data = tomllib.load(f)
    cfg = GridConfig.model_validate(data)
    assert cfg.x.param == CutParam.POWER_PCT
    assert cfg.y.param == CutParam.Z_PER_PASS_MM
    assert cfg.border.enabled is True
    assert cfg.border.power_pct == 10.0


def test_example_config_3d_parses():
    """example_config_3d.toml must parse and produce a valid GridConfig with cols."""
    path = runfiles.get_required_path("_main/laser/material_test/example_config_3d.toml")
    with path.open("rb") as f:
        data = tomllib.load(f)
    cfg = GridConfig.model_validate(data)
    assert cfg.cols is not None
    assert cfg.cols.param == CutParam.NUM_PASSES


# ── 3D/4D sweep tests ────────────────────────────────────────────────────────


def test_grid_config_3param_cols_only():
    cfg = _minimal_config(cols=AxisConfig(param=CutParam.NUM_PASSES, values=[1, 2, 3]))
    assert cfg.cols is not None
    assert len(cfg.cols.values) == 3


def test_grid_config_3param_rows_only():
    cfg = _minimal_config(rows=AxisConfig(param=CutParam.Z_PER_PASS_MM, values=[-0.3, -0.5]))
    assert cfg.rows is not None
    assert len(cfg.rows.values) == 2


def test_grid_config_4param():
    cfg = _minimal_config(
        cols=AxisConfig(param=CutParam.NUM_PASSES, values=[1, 2]),
        rows=AxisConfig(param=CutParam.Z_PER_PASS_MM, values=[-0.3, -0.5]),
    )
    assert cfg.cols is not None
    assert cfg.rows is not None


def test_grid_config_rejects_duplicate_cols_param():
    """cols.param must differ from x.param and y.param."""
    with pytest.raises(Exception, match="must be different"):
        _minimal_config(cols=AxisConfig(param=CutParam.POWER_PCT, values=[10, 20]))


def test_grid_config_rejects_duplicate_rows_param():
    """rows.param must differ from x.param and y.param."""
    with pytest.raises(Exception, match="must be different"):
        _minimal_config(rows=AxisConfig(param=CutParam.SPEED_MM_S, values=[50, 100]))


def test_grid_config_rejects_duplicate_cols_rows_param():
    """cols.param and rows.param must be different from each other."""
    with pytest.raises(Exception, match="must be different"):
        _minimal_config(
            cols=AxisConfig(param=CutParam.NUM_PASSES, values=[1, 2]),
            rows=AxisConfig(param=CutParam.NUM_PASSES, values=[3, 4]),
        )


def test_generate_3d_layer_count():
    """3D sweep: text layer + (inner_cells * n_outer_cols) cell layers."""
    cfg = _minimal_config(cols=AxisConfig(param=CutParam.NUM_PASSES, values=[1, 2, 3]))
    project = generate(cfg)
    inner_cells = 3 * 2  # x=3, y=2
    n_outer_cols = 3
    assert len(project.cut_settings) == 1 + inner_cells * n_outer_cols


def test_generate_3d_rect_count():
    """3D sweep: one rect per cell across all sub-grids."""
    cfg = _minimal_config(cols=AxisConfig(param=CutParam.NUM_PASSES, values=[1, 2, 3]))
    project = generate(cfg)
    rects = [s for s in project.shapes if isinstance(s, RectShape)]
    assert len(rects) == 3 * 2 * 3  # inner_cols * inner_rows * outer_cols


def test_generate_4d_layer_count():
    cfg = _minimal_config(
        cols=AxisConfig(param=CutParam.NUM_PASSES, values=[1, 2]),
        rows=AxisConfig(param=CutParam.Z_PER_PASS_MM, values=[-0.3, -0.5]),
    )
    project = generate(cfg)
    inner_cells = 3 * 2
    total_subgrids = 2 * 2
    assert len(project.cut_settings) == 1 + inner_cells * total_subgrids


def test_generate_4d_rect_count():
    cfg = _minimal_config(
        cols=AxisConfig(param=CutParam.NUM_PASSES, values=[1, 2]),
        rows=AxisConfig(param=CutParam.Z_PER_PASS_MM, values=[-0.3, -0.5]),
    )
    project = generate(cfg)
    rects = [s for s in project.shapes if isinstance(s, RectShape)]
    assert len(rects) == 3 * 2 * 2 * 2


def test_generate_3d_outer_params_applied():
    """Outer cols param (num_passes) should be applied to each sub-grid's cells."""
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0, 20.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0]),
        cols=AxisConfig(param=CutParam.NUM_PASSES, values=[1, 3]),
    )
    project = generate(cfg)
    # Skip text layer (index 0)
    cell_layers = [cs for cs in project.cut_settings if cs.index > 0]
    # First sub-grid (cols[0]=1 pass): 2 cells
    assert cell_layers[0].num_passes == 1
    assert cell_layers[1].num_passes == 1
    # Second sub-grid (cols[1]=3 passes): 2 cells
    assert cell_layers[2].num_passes == 3
    assert cell_layers[3].num_passes == 3


def test_generate_4d_outer_params_applied():
    """Both cols and rows params should be applied.

    Global energy sort: power=10, speed=50 for all cells, so energy depends
    only on num_passes. passes=1 cells (energy=0.2) sort before passes=2
    cells (energy=0.4). Within same energy, stable sort preserves iteration
    order (row0 before row1).
    """
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0]),
        cols=AxisConfig(param=CutParam.NUM_PASSES, values=[1, 2]),
        rows=AxisConfig(param=CutParam.Z_PER_PASS_MM, values=[-0.3, -0.5]),
    )
    project = generate(cfg)
    cell_layers = [cs for cs in project.cut_settings if cs.index > 0]
    # 4 sub-grids x 1 cell each = 4 cells, sorted by energy globally
    # passes=1 cells first (energy=0.2), then passes=2 (energy=0.4)
    assert cell_layers[0].num_passes == 1
    assert cell_layers[0].z_per_pass == -0.3
    assert cell_layers[1].num_passes == 1
    assert cell_layers[1].z_per_pass == -0.5
    assert cell_layers[2].num_passes == 2
    assert cell_layers[2].z_per_pass == -0.3
    assert cell_layers[3].num_passes == 2
    assert cell_layers[3].z_per_pass == -0.5


def test_generate_2d_backward_compat():
    """2D generation (no cols/rows) should produce same output as before."""
    project = _make_project()
    # Layer 0 (text) + 6 cells
    assert len(project.cut_settings) == 7
    rects = [s for s in project.shapes if isinstance(s, RectShape)]
    assert len(rects) == 6
    # Verify it produces valid XML
    xml_str = project.to_xml_str()
    root = ET.fromstring(xml_str.split("\n", 1)[1])
    assert root.tag == "LightBurnProject"


def test_generate_3d_border():
    cfg = _minimal_config(cols=AxisConfig(param=CutParam.NUM_PASSES, values=[1, 2]), border=BorderConfig(enabled=True))
    project = generate(cfg)
    assert any(cs.name == "Border" for cs in project.cut_settings)
    rects = [s for s in project.shapes if isinstance(s, RectShape)]
    inner_rects = 3 * 2 * 2  # x * y * outer_cols
    assert len(rects) == inner_rects + 1  # +1 for border


def test_auto_subtitle_excludes_outer_axes():
    """Auto-subtitle should not mention cols or rows parameters."""
    cfg = GridConfig(
        title="T",
        auto_subtitle=True,
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0]),
        cols=AxisConfig(param=CutParam.NUM_PASSES, values=[1, 2]),
        rows=AxisConfig(param=CutParam.Z_PER_PASS_MM, values=[-0.3]),
    )
    sub = _full_subtitle(cfg)
    assert "Passes" not in sub
    assert "Z/pass" not in sub


def test_generate_3d_from_toml():
    """3D config should parse from TOML correctly."""
    toml_str = textwrap.dedent("""\
        title = "3D Test"

        [x]
        param = "power_pct"
        values = [10.0, 20.0]

        [y]
        param = "speed_mm_s"
        values = [50.0, 100.0]

        [cols]
        param = "num_passes"
        values = [1, 2, 3]
    """)
    data = tomllib.loads(toml_str)
    cfg = GridConfig.model_validate(data)
    assert cfg.cols is not None
    assert cfg.cols.param == CutParam.NUM_PASSES
    assert cfg.cols.values == [1, 2, 3]

    project = generate(cfg)
    cell_layers = [cs for cs in project.cut_settings if cs.index > 0]
    assert len(cell_layers) == 2 * 2 * 3  # x * y * cols


def test_generate_rows_only():
    """rows-only (no cols) should work as a 3D sweep."""
    cfg = _minimal_config(rows=AxisConfig(param=CutParam.Z_PER_PASS_MM, values=[-0.3, -0.5]))
    project = generate(cfg)
    cell_layers = [cs for cs in project.cut_settings if cs.index > 0]
    assert len(cell_layers) == 3 * 2 * 2  # x * y * rows


def test_subgrid_gap_affects_layout():
    """Different subgrid_gap_mm should produce different rect positions."""
    cfg_small = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0]),
        cols=AxisConfig(param=CutParam.NUM_PASSES, values=[1, 2]),
        geometry=GeometryConfig(subgrid_gap_mm=5.0),
    )
    cfg_large = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0]),
        cols=AxisConfig(param=CutParam.NUM_PASSES, values=[1, 2]),
        geometry=GeometryConfig(subgrid_gap_mm=50.0),
    )
    p_small = generate(cfg_small)
    p_large = generate(cfg_large)

    def rect_xs(project: LightBurnProject) -> list[float]:
        return sorted(s.xform.tx for s in project.shapes if isinstance(s, RectShape))

    xs_small = rect_xs(p_small)
    xs_large = rect_xs(p_large)
    # Larger gap → second sub-grid's rect is further to the right
    assert xs_large[1] > xs_small[1]


# ── _param_short_label ──────────────────────────────────────────────────────────


def test_param_short_label():
    assert _param_short_label(CutParam.SPEED_MM_S) == "mm/s"  # abbreviable + unit
    assert _param_short_label(CutParam.POWER_PCT) == "PWR%"  # not abbreviable + unit
    assert _param_short_label(CutParam.NUM_PASSES) == "Passes"  # no unit


# ── Legend cell ─────────────────────────────────────────────────────────────────


def test_generate_legend_cell():
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0, 20.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0]),
        labels=LabelsConfig(cell_text=CellContent.VALUES_WITH_UNITS, show_legend=True),
    )
    project = generate(cfg)
    # Legend rect on layer 0
    legend_rects = [s for s in project.shapes if isinstance(s, RectShape) and s.cut_index == 0]
    assert len(legend_rects) == 1
    # Two legend text labels
    texts = [s for s in project.shapes if isinstance(s, TextShape)]
    legend_texts = [t for t in texts if t.text in ("PWR%", "mm/s")]
    assert len(legend_texts) == 2


def test_generate_legend_not_shown_when_no_cell_content():
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0, 20.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0]),
        labels=LabelsConfig(cell_text=CellContent.NOTHING, show_legend=True),
    )
    project = generate(cfg)
    legend_rects = [s for s in project.shapes if isinstance(s, RectShape) and s.cut_index == 0]
    assert len(legend_rects) == 0


def test_generate_legend_shown_when_labels_hidden():
    """Legend is shown even when axis labels are disabled."""
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0, 20.0], show_labels=False),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0], show_labels=False),
        labels=LabelsConfig(cell_text=CellContent.VALUES_WITH_UNITS, show_legend=True),
        auto_subtitle=False,
    )
    project = generate(cfg)
    legend_rects = [s for s in project.shapes if isinstance(s, RectShape) and s.cut_index == 0]
    assert len(legend_rects) == 1


def test_generate_legend_default_off():
    """Default show_legend=False should not add legend rect."""
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0, 20.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0]),
        labels=LabelsConfig(cell_text=CellContent.VALUES_WITH_UNITS),
    )
    project = generate(cfg)
    legend_rects = [s for s in project.shapes if isinstance(s, RectShape) and s.cut_index == 0]
    assert len(legend_rects) == 0


if __name__ == "__main__":
    pytest_bazel.main()
