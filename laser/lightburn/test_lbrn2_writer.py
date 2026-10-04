import math
from xml.etree import ElementTree as ET

import pytest
import pytest_bazel

from laser.lightburn.lbrn2_writer import (
    MAX_LAYERS,
    CutMode,
    CutSetting,
    HAlign,
    LightBurnProject,
    RectShape,
    SubLayer,
    TextShape,
    VAlign,
    XForm,
)


def test_sublayer_to_element_minimal():
    sl = SubLayer(index=1, max_power=30, speed=200)
    el = sl.to_element()

    assert el.tag == "SubLayer"
    assert el.attrib["type"] == "Cut"
    assert el.attrib["index"] == "1"
    children = {child.tag: child.attrib["Value"] for child in el}
    assert children["maxPower"] == "30"
    assert children["speed"] == "200"
    assert "minPower" not in children
    assert "subname" not in children


def test_sublayer_to_element_with_subname():
    sl = SubLayer(index=2, mode=CutMode.SCAN, subname="fast pass", speed=500)
    el = sl.to_element()

    assert el.attrib["type"] == "Scan"
    assert el.attrib["index"] == "2"
    children = {child.tag: child.attrib["Value"] for child in el}
    assert children["subname"] == "fast pass"
    assert children["speed"] == "500"


def test_cutsetting_with_sublayers():
    cs = CutSetting(
        index=0,
        name="C00",
        max_power=20,
        speed=100,
        subname="main pass",
        sublayers=[SubLayer(index=1, max_power=30, speed=200, subname="second pass")],
    )
    el = cs.to_element()

    # Parent has subname
    subname_els = [c for c in el if c.tag == "subname"]
    assert len(subname_els) == 1
    assert subname_els[0].attrib["Value"] == "main pass"

    # One SubLayer child
    sublayer_els = [c for c in el if c.tag == "SubLayer"]
    assert len(sublayer_els) == 1
    sl_el = sublayer_els[0]
    assert sl_el.attrib["index"] == "1"
    sl_children = {child.tag: child.attrib["Value"] for child in sl_el}
    assert sl_children["maxPower"] == "30"
    assert sl_children["speed"] == "200"
    assert sl_children["subname"] == "second pass"


def test_max_layers_at_limit():
    """Exactly MAX_LAYERS cut settings should succeed."""
    project = LightBurnProject(cut_settings=[CutSetting(index=i, name=f"C{i:02d}") for i in range(MAX_LAYERS)])
    root = project.to_element()
    assert root.tag == "LightBurnProject"


def test_max_layers_exceeded():
    """More than MAX_LAYERS cut settings should raise ValueError."""
    project = LightBurnProject(cut_settings=[CutSetting(index=i, name=f"C{i:02d}") for i in range(MAX_LAYERS + 1)])
    with pytest.raises(ValueError, match=f"at most {MAX_LAYERS} layers, got {MAX_LAYERS + 1}"):
        project.to_element()


def test_sublayers_dont_count_toward_limit():
    """Sublayers within a CutSetting don't count toward the layer limit."""
    project = LightBurnProject(
        cut_settings=[
            CutSetting(index=i, name=f"C{i:02d}", sublayers=[SubLayer(index=j) for j in range(1, 4)])
            for i in range(MAX_LAYERS)
        ]
    )
    root = project.to_element()
    assert root.tag == "LightBurnProject"


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
    # ElementTree escapes at serialisation, so the attribute must hold the raw string.
    assert el.attrib["Str"] == 'A & B < C > "D"'


def test_text_shape_rotated():
    txt = TextShape(
        cut_index=0, text="Y label", height=6.0, xform=XForm.rotate90ccw(10.0, 80.0), ah=HAlign.CENTER, av=VAlign.CENTER
    )
    el = txt.to_element()
    xform_el = el.find("XForm")
    assert xform_el is not None
    assert xform_el.text == "0 1 -1 0 10 80"


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


if __name__ == "__main__":
    pytest_bazel.main()
