import textwrap
from pathlib import Path

import pydantic
import pytest
import pytest_bazel

from util.testing.visual_scenarios import Click, Scenario, Viewport, load_scenarios


def test_scenarios_load_in_table_order_with_harness_only_fields_ignored(tmp_path: Path) -> None:
    table = tmp_path / "scenarios.json"
    table.write_text(
        textwrap.dedent(
            """\
            {
              "second": {"element": "#app", "route": "/threads/1", "openSettings": true},
              "first": {
                "element": "#shot",
                "viewport": {"width": 412, "height": 915, "deviceScaleFactor": 2.625, "hasTouch": true},
                "outputName": "first-phone",
                "label": "First · phone",
                "query": {"scene": "alpha", "variant": "narrow"},
                "colorScheme": "dark",
                "readySelectors": ["#a", "#b"],
                "captureViewport": true,
                "windowGlobals": {"__FIXTURE__": 3, "__VARIANT__": "compact"},
                "hover": "#h",
                "readyFrames": {"iframe.child": "main"},
                "clicks": [
                  {"selector": "#open", "expectVisible": ["#panel"], "expectHidden": ["#spinner"]},
                  {"selector": "#next", "expectHidden": ["#panel"]}
                ],
                "hiddenSelectors": [".loading"],
                "scrollToBottom": ".scroller"
              }
            }
            """
        ),
        encoding="utf-8",
    )

    scenarios = load_scenarios(table)

    assert list(scenarios) == ["second", "first"]
    assert scenarios["first"] == Scenario(
        element="#shot",
        viewport=Viewport(width=412, height=915, device_scale_factor=2.625, has_touch=True),
        output_name="first-phone",
        label="First · phone",
        query={"scene": "alpha", "variant": "narrow"},
        color_scheme="dark",
        ready_selectors=["#a", "#b"],
        capture_viewport=True,
        window_globals={"__FIXTURE__": 3, "__VARIANT__": "compact"},
        hover="#h",
        ready_frames={"iframe.child": "main"},
        clicks=[
            Click(selector="#open", expect_visible=["#panel"], expect_hidden=["#spinner"]),
            Click(selector="#next", expect_hidden=["#panel"]),
        ],
        hidden_selectors=[".loading"],
        scroll_to_bottom=".scroller",
    )


def test_a_viewport_states_only_what_differs_from_the_default() -> None:
    scenario = Scenario.model_validate({"element": "#app", "viewport": {"height": 1500}})

    assert scenario.viewport == Viewport(width=1200, height=1500)


def test_every_scenario_states_its_element(tmp_path: Path) -> None:
    table = tmp_path / "scenarios.json"
    table.write_text('{"no_element": {"readySelectors": ["#a"]}}')

    with pytest.raises(pydantic.ValidationError, match=r"no_element\.element"):
        load_scenarios(table)


def test_tap_needs_a_touch_viewport() -> None:
    with pytest.raises(pydantic.ValidationError, match=r"tap needs viewport\.hasTouch"):
        Scenario.model_validate({"element": "#app", "tap": "#button"})


def test_a_click_states_what_it_changes() -> None:
    with pytest.raises(pydantic.ValidationError, match=r"click '#open' must state what it changes"):
        Scenario.model_validate({"element": "#app", "clicks": [{"selector": "#open"}]})


def test_a_click_names_a_selector() -> None:
    with pytest.raises(pydantic.ValidationError, match="selector"):
        Click.model_validate({"expectVisible": ["#panel"]})


def test_window_globals_are_assigned_by_name_so_a_name_must_be_an_identifier() -> None:
    with pytest.raises(pydantic.ValidationError, match=r"window globals must be JavaScript identifiers: .*not-a-name"):
        Scenario.model_validate({"element": "#app", "windowGlobals": {"__OK__": 1, "not-a-name": 2}})


def test_unsupported_color_scheme_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError, match="colorScheme"):
        Scenario.model_validate({"element": "#app", "colorScheme": "sepia"})


if __name__ == "__main__":
    pytest_bazel.main()
