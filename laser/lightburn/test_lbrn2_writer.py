import pytest
import pytest_bazel

from laser.lightburn.lbrn2_writer import MAX_LAYERS, CutMode, CutSetting, LightBurnProject, SubLayer


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


if __name__ == "__main__":
    pytest_bazel.main()
