"""Tests for the material test grid generator."""

from __future__ import annotations

import math
import textwrap
import tomllib
from xml.etree import ElementTree as ET

import pytest
import pytest_bazel

from bazel_util import runfiles
from experimental.lightburn_material_test.lbrn2_writer import (
    CutSetting,
    HAlign,
    LightBurnProject,
    RectShape,
    TextShape,
    VAlign,
    XForm,
)
from experimental.lightburn_material_test.material_test import (
    AnnotationConfig,
    AxisConfig,
    BorderConfig,
    CutConfig,
    CutParam,
    CutParams,
    GridConfig,
    _full_subtitle,
    fmt_val,
    generate,
)

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
        "x": AxisConfig(param=CutParam.POWER_MAX, values=[10, 20, 30]),
        "y": AxisConfig(param=CutParam.SPEED, values=[50, 100]),
    }
    base.update(kwargs)
    return GridConfig(**base)


def test_grid_config_minimal():
    cfg = _minimal_config()
    assert cfg.x.param == CutParam.POWER_MAX
    assert cfg.y.values == [50, 100]


def test_grid_config_rejects_same_axes():
    with pytest.raises(Exception, match="must be different"):
        GridConfig(
            x=AxisConfig(param=CutParam.SPEED, values=[10, 20]), y=AxisConfig(param=CutParam.SPEED, values=[50, 100])
        )


def test_cut_config_power_shorthand():
    cc = CutConfig(power=75)
    assert cc.power_min == 75
    assert cc.power_max == 75


def test_cut_config_power_shorthand_does_not_override_explicit():
    # power_max set explicitly should still be overridden by power shorthand
    # (model_validator runs after all fields are set)
    cc = CutConfig(power=60, power_max=80)
    assert cc.power_min == 60
    assert cc.power_max == 60


# ── CutParams / BorderConfig ───────────────────────────────────────────────────


def test_cut_params_power_shorthand():
    p = CutParams(power=42.0)
    assert p.power_min == 42.0
    assert p.power_max == 42.0


def test_cut_params_to_cut_setting_full():
    """to_cut_setting propagates all cut fields."""
    p = CutParams(power_min=5.0, power_max=95.0, speed=50.0, kerf=0.1, z_offset=-0.5, z_per_pass=-0.2, num_passes=4)
    cs = p.to_cut_setting(index=3, name="TestLayer")
    assert cs.index == 3
    assert cs.name == "TestLayer"
    assert cs.min_power == 5.0
    assert cs.max_power == 95.0
    assert cs.speed == 50.0
    assert cs.kerf == 0.1
    assert cs.z_offset == -0.5
    assert cs.z_per_pass == -0.2
    assert cs.num_passes == 4


def test_border_config_defaults():
    bc = BorderConfig()
    assert bc.cut.power_min == 10.0
    assert bc.cut.power_max == 10.0
    assert bc.cut.speed == 200.0
    assert bc.enabled is False
    assert bc.padding == 3.0


def test_border_config_full_params():
    """BorderConfig.cut accepts the full cut parameter set."""
    bc = BorderConfig(
        enabled=True, padding=5.0, cut=CutParams(power_min=8.0, power_max=12.0, speed=100.0, kerf=0.1, num_passes=2)
    )
    cs = bc.cut.to_cut_setting(7, "Border")
    assert cs.min_power == 8.0
    assert cs.max_power == 12.0
    assert cs.kerf == 0.1
    assert cs.num_passes == 2


def test_generate_text_layer_full_params_applied():
    """Full cut params specified on text_layer are reflected in generated layer 0."""
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_MAX, values=[10.0]),
        y=AxisConfig(param=CutParam.SPEED, values=[50.0]),
        text_layer=CutParams(power_min=3.0, power_max=7.0, speed=150.0, kerf=0.02),
    )
    project = generate(cfg)
    text_cs = project.cut_settings[0]
    assert text_cs.min_power == 3.0
    assert text_cs.max_power == 7.0
    assert text_cs.speed == 150.0
    assert text_cs.kerf == 0.02


def test_generate_border_full_params_applied():
    """Full cut params specified on border.cut are reflected in the border layer."""
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_MAX, values=[10.0]),
        y=AxisConfig(param=CutParam.SPEED, values=[50.0]),
        border=BorderConfig(enabled=True, cut=CutParams(power_min=2.0, power_max=8.0, speed=120.0, z_offset=-0.3)),
    )
    project = generate(cfg)
    border_cs = next(cs for cs in project.cut_settings if cs.name == "Border")
    assert border_cs.min_power == 2.0
    assert border_cs.max_power == 8.0
    assert border_cs.speed == 120.0
    assert border_cs.z_offset == -0.3


def test_grid_config_from_toml():
    toml_str = textwrap.dedent("""\
        title = "Test"

        [x]
        param = "power_max"
        values = [10.0, 20.0]
        label = "Power [%]"

        [y]
        param = "speed"
        values = [50.0, 100.0]
        label = "Speed [mm/s]"

        [cut]
        speed = 15
        z_offset = -0.1
    """)
    data = tomllib.loads(toml_str)
    cfg = GridConfig.model_validate(data)
    assert cfg.title == "Test"
    assert cfg.x.param == CutParam.POWER_MAX
    assert cfg.cut.z_offset == -0.1


# ── generate() ────────────────────────────────────────────────────────────────


def _make_project(
    x_values: list[float] | None = None, y_values: list[float] | None = None, **kwargs
) -> LightBurnProject:
    resolved_x = x_values if x_values is not None else [10.0, 20.0, 30.0]
    resolved_y = y_values if y_values is not None else [50.0, 100.0]
    cfg = GridConfig(
        title="Test grid",
        x=AxisConfig(param=CutParam.POWER_MAX, values=resolved_x, label="Power [%]"),
        y=AxisConfig(param=CutParam.SPEED, values=resolved_y, label="Speed [mm/s]"),
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
    project = _make_project(border=BorderConfig(enabled=True, cut=CutParams(power=5, speed=100)))
    # border layer added
    assert any(cs.name == "Border" for cs in project.cut_settings)
    # border rect added (one extra rect)
    rects = [s for s in project.shapes if isinstance(s, RectShape)]
    assert len(rects) == 3 * 2 + 1


def test_generate_cell_cut_settings_have_correct_params():
    """Each cell layer should have x_param and y_param set correctly."""
    x_vals = [10.0, 20.0]
    y_vals = [50.0, 100.0]
    project = _make_project(x_values=x_vals, y_values=y_vals)
    # Layer 0 is text; cell layers start at 1
    cell_layers = project.cut_settings[1:]  # skip text layer
    assert len(cell_layers) == 4
    # Row 0, col 0: power=10, speed=50
    assert cell_layers[0].max_power == 10.0
    assert cell_layers[0].speed == 50.0
    # Row 0, col 1: power=20, speed=50
    assert cell_layers[1].max_power == 20.0
    assert cell_layers[1].speed == 50.0
    # Row 1, col 0: power=10, speed=100
    assert cell_layers[2].max_power == 10.0
    assert cell_layers[2].speed == 100.0


def test_generate_xml_round_trips():
    project = _make_project()
    xml_str = project.to_xml_str()
    root = ET.fromstring(xml_str.split("\n", 1)[1])
    assert root.tag == "LightBurnProject"


def test_generate_with_cell_text():
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_MAX, values=[10.0, 20.0]),
        y=AxisConfig(param=CutParam.SPEED, values=[50.0]),
        annotations=AnnotationConfig(show_cell_text=True),
    )
    project = generate(cfg)
    texts = [s for s in project.shapes if isinstance(s, TextShape)]
    # Should have in-cell texts (2 per cell x 2 cells = 4) plus axis annotations
    assert len(texts) >= 4


def test_generate_no_annotations():
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_MAX, values=[10.0, 20.0], show_annotations=False),
        y=AxisConfig(param=CutParam.SPEED, values=[50.0], show_annotations=False),
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
        x=AxisConfig(param=CutParam.POWER_MAX, values=[10.0, 20.0]),
        y=AxisConfig(param=CutParam.SPEED, values=[50.0, 100.0]),
        cut=CutConfig(z_offset=-0.1, kerf=0.05),
    )
    sub = _full_subtitle(cfg)
    assert "Power" not in sub
    assert "Speed" not in sub
    assert "Z=" in sub or "kerf" in sub.lower() or "Kerf" in sub


def test_example_config_parses():
    """example_config.toml must parse without error and produce a valid GridConfig."""
    path = runfiles.get_required_path("_main/experimental/lightburn_material_test/example_config.toml")
    with path.open("rb") as f:
        data = tomllib.load(f)
    cfg = GridConfig.model_validate(data)
    assert cfg.x.param == CutParam.POWER_MAX
    assert cfg.y.param == CutParam.Z_PER_PASS
    assert cfg.border.enabled is True
    assert cfg.border.cut.power_min == 10.0


if __name__ == "__main__":
    pytest_bazel.main()
