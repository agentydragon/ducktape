import json
import shutil
import struct
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_bazel
from matplotlib import get_data_path
from playwright.async_api import (
    Playwright,
    TimeoutError as PlaywrightTimeoutError,  # the builtin TimeoutError is another type
)

from util.testing import visual_sweep
from util.testing.visual_scenarios import Scenario, Viewport
from util.testing.visual_sweep import SweepConfig, capture_scenario
from util.visual_review import VisualReviewManifest

# gazelle:include_dep //util:playwright

pytest_plugins = ("util.playwright", "pytester")

# What each scene of the test harness page shows, by `?page=` name (or `?scene=`, for a scenario that
# names its own query). The scenes that fail do so in the way their name says; `index.html` and this
# file are all the harness there is.
_HARNESS_JS = """
const params = new URLSearchParams(location.search);
const scene = params.get("page") ?? params.get("scene");
const app = document.getElementById("app");
const shot = '<div id="shot"><div id="target"></div></div>';
if (scene === "plain") app.innerHTML = shot;
if (scene === "typeset") app.innerHTML = shot + '<span class="typeset">A</span>';
if (scene === "typeset_late") setTimeout(() => { app.innerHTML = shot + '<span class="typeset">A</span>'; }, 1000);
if (scene === "late") setTimeout(() => { app.innerHTML = shot + '<p class="arrived"></p>'; }, 100);
if (scene === "throws") { app.innerHTML = shot; setTimeout(() => { throw new Error("scene exploded"); }); }
if (scene === "escapes") app.innerHTML = shot + '<img src="http://fenced.test/x.png">';
if (scene === "ledger") {
  app.innerHTML = shot;
  window.__visualNetworkLedger__ = { pending: [], violations: ["unmatched route /api/x"] };
}
if (scene === "interactive") {
  app.innerHTML = shot;
  document.getElementById("target").addEventListener("click", (event) => event.target.classList.add("tapped"));
}
"""
_INDEX_HTML = textwrap.dedent(
    """\
    <!doctype html>
    <style>
      @font-face { font-family: "Declared Sans"; src: url("./missing.woff2"); }
      @font-face { font-family: "Loaded Sans"; src: url("./loaded.ttf"); }
      .typeset { font-family: "Loaded Sans", sans-serif; }
      body { margin: 0; background: Canvas; color: CanvasText; color-scheme: light dark; }
      #shot { width: 100px; height: 40px; margin: 60px; }
      #target { width: 100px; height: 40px; background: #3366cc; }
      #target:hover { background: #cc6633; }
      #target.tapped { background: #33cc66; }
    </style>
    <div id="app"></div>
    <script src="./harness.js"></script>
    """
)


@pytest.fixture
def config(tmp_path: Path) -> SweepConfig:
    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "index.html").write_text(_INDEX_HTML)
    (harness / "harness.js").write_text(_HARNESS_JS)
    # Any real font file will do; matplotlib ships one.
    shutil.copy(Path(get_data_path()) / "fonts" / "ttf" / "DejaVuSans.ttf", harness / "loaded.ttf")
    return SweepConfig(
        harness_path=harness / "harness.js",
        scenarios_path=tmp_path / "scenarios.json",
        title="Test sweep",
        expected_font_family=None,
        output_suffix="-actual",
    )


def _png_size(png: bytes) -> tuple[int, int]:
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def _published(directory: Path) -> dict[str, bytes]:
    """The PNGs a sweep left in `directory`, by file name."""
    return {path.name: path.read_bytes() for path in directory.glob("*.png")}


def _nothing_published(directory: Path) -> bool:
    return not _published(directory) and not (directory / "visual-review.json").exists()


def _manifest(directory: Path) -> VisualReviewManifest:
    return VisualReviewManifest.model_validate_json((directory / "visual-review.json").read_text())


async def test_a_scenario_publishes_its_png_and_manifest_entry(
    playwright: Playwright, config: SweepConfig, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()

    await capture_scenario(
        playwright,
        "plain",
        Scenario(element="#shot", viewport=Viewport(device_scale_factor=2), output_name="renamed"),
        config=config,
        output_dir=out,
    )
    await capture_scenario(
        playwright, "plain", Scenario(element="#app", capture_viewport=True), config=config, output_dir=out
    )

    # Device pixels, and the viewport when asked for it rather than the element.
    published = _published(out)
    assert _png_size(published["renamed-actual.png"]) == (200, 80)
    assert _png_size(published["plain-actual.png"]) == (1200, 800)
    manifest = _manifest(out)
    assert manifest.title == "Test sweep"
    assert [(asset.path, asset.label) for asset in manifest.assets] == [
        ("renamed-actual.png", "renamed"),
        ("plain-actual.png", "plain"),
    ]


async def test_a_scenario_can_name_its_harness_query_and_its_manifest_label(
    playwright: Playwright, config: SweepConfig, tmp_path: Path
) -> None:
    # Neither the scenario name nor `?page=` is a scene of the harness: only `?scene=plain` mounts one.
    await capture_scenario(
        playwright,
        "plain_wide",
        Scenario(element="#shot", query={"scene": "plain"}, label="plain · wide"),
        config=config,
        output_dir=tmp_path,
        timeout_ms=1000,
    )

    assert [(asset.path, asset.label) for asset in _manifest(tmp_path).assets] == [
        ("plain_wide-actual.png", "plain · wide")
    ]


async def test_a_lane_can_publish_bare_png_names(playwright: Playwright, config: SweepConfig, tmp_path: Path) -> None:
    await capture_scenario(
        playwright, "plain", Scenario(element="#shot"), config=replace(config, output_suffix=""), output_dir=tmp_path
    )

    assert [asset.path for asset in _manifest(tmp_path).assets] == list(_published(tmp_path)) == ["plain.png"]


async def test_the_color_scheme_reaches_the_page(playwright: Playwright, config: SweepConfig, tmp_path: Path) -> None:
    for scheme in ("light", "dark"):
        out = tmp_path / scheme
        out.mkdir()
        await capture_scenario(
            playwright, "plain", Scenario(element="#app", color_scheme=scheme), config=config, output_dir=out
        )

    assert _published(tmp_path / "light") != _published(tmp_path / "dark")


async def test_a_scenario_waits_for_its_ready_selectors(
    playwright: Playwright, config: SweepConfig, tmp_path: Path
) -> None:
    await capture_scenario(
        playwright, "late", Scenario(element="#shot", ready_selectors=[".arrived"]), config=config, output_dir=tmp_path
    )

    assert list(_published(tmp_path)) == ["late-actual.png"]


async def test_hover_and_tap_are_in_the_capture(playwright: Playwright, config: SweepConfig, tmp_path: Path) -> None:
    scenarios = [
        Scenario(element="#target", output_name="idle"),
        Scenario(element="#target", output_name="hover", hover="#target"),
        Scenario(element="#target", output_name="tap", viewport=Viewport(has_touch=True), tap="#target"),
    ]
    for scenario in scenarios:
        await capture_scenario(playwright, "interactive", scenario, config=config, output_dir=tmp_path)

    renders = _published(tmp_path)
    assert sorted(renders) == ["hover-actual.png", "idle-actual.png", "tap-actual.png"]
    assert renders["hover-actual.png"] != renders["idle-actual.png"], "hover"
    assert renders["tap-actual.png"] != renders["idle-actual.png"], "tap"


@pytest.mark.parametrize(
    ("page_name", "scenario", "failure"),
    [
        pytest.param(
            "late",
            Scenario(element="#shot", ready_selectors=[".never-arrives"]),
            (PlaywrightTimeoutError, r"\.never-arrives"),
            id="a ready selector that never appears",
        ),
        pytest.param(
            "throws",
            Scenario(element="#shot"),
            (AssertionError, r"uncaught page errors:\n\s+Error: scene exploded"),
            id="an uncaught page error",
        ),
        pytest.param(
            "escapes",
            Scenario(element="#shot"),
            (AssertionError, r"requests escaped the harness:\n\s+image http://fenced.test/x.png"),
            id="a request that leaves the harness",
        ),
        pytest.param(
            "ledger",
            Scenario(element="#shot"),
            (AssertionError, r"network violations:\n\s+unmatched route /api/x"),
            id="a violation the network ledger recorded",
        ),
        pytest.param(
            "plain",
            Scenario(element="#absent"),
            (LookupError, r"selector='#absent' matched no element"),
            id="an element that is not there",
        ),
    ],
)
async def test_an_unhealthy_scenario_fails_by_name_and_publishes_nothing(
    playwright: Playwright,
    config: SweepConfig,
    tmp_path: Path,
    page_name: str,
    scenario: Scenario,
    failure: tuple[type[Exception], str],
) -> None:
    error_type, message = failure

    with pytest.raises(error_type, match=message):
        await capture_scenario(playwright, page_name, scenario, config=config, output_dir=tmp_path, timeout_ms=1000)

    assert _nothing_published(tmp_path)


async def test_a_named_font_that_loaded_passes_and_is_published(
    playwright: Playwright, config: SweepConfig, tmp_path: Path
) -> None:
    await capture_scenario(
        playwright,
        "typeset",
        Scenario(element="#shot"),
        config=replace(config, expected_font_family="Loaded Sans"),
        output_dir=tmp_path,
    )

    assert list(_published(tmp_path)) == ["typeset-actual.png"]


async def test_a_font_is_asserted_of_the_mounted_scene_not_the_page_at_network_idle(
    playwright: Playwright, config: SweepConfig, tmp_path: Path
) -> None:
    # The scene mounts after the 500ms of quiet that ends the navigation, and a face loads only once
    # text uses it: asked at network idle, the font would be declared and not yet loaded.
    await capture_scenario(
        playwright,
        "typeset_late",
        Scenario(element="#shot", ready_selectors=[".typeset"]),
        config=replace(config, expected_font_family="Loaded Sans"),
        output_dir=tmp_path,
    )

    assert list(_published(tmp_path)) == ["typeset_late-actual.png"]


@pytest.mark.parametrize(
    ("page_name", "family", "status"),
    [
        pytest.param("plain", "Declared Sans", "unloaded", id="declared but never loaded"),
        # `document.fonts.check` alone is true for a family nothing declares: a stylesheet that never
        # arrived, or a misspelled family, would pass it and render in the fallback font.
        pytest.param("typeset", "Loaded Snas", "undeclared", id="declared by nothing"),
    ],
)
async def test_a_named_font_that_did_not_load_fails_the_scenario(
    playwright: Playwright, config: SweepConfig, tmp_path: Path, page_name: str, family: str, status: str
) -> None:
    with pytest.raises(AssertionError, match=rf"{page_name}: {family} font did not load \({status}\)"):
        await capture_scenario(
            playwright,
            page_name,
            Scenario(element="#shot"),
            config=replace(config, expected_font_family=family),
            output_dir=tmp_path,
        )

    assert _nothing_published(tmp_path)


def _sweep(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, config: SweepConfig, table: dict[str, dict]
) -> Path:
    config.scenarios_path.write_text(json.dumps(table))
    out = pytester.path / "out"
    monkeypatch.setenv("HARNESS_PATH", str(config.harness_path))
    monkeypatch.setenv("SCENARIOS_PATH", str(config.scenarios_path))
    monkeypatch.setenv("VISUAL_TITLE", config.title)
    monkeypatch.setenv("TEST_UNDECLARED_OUTPUTS_DIR", str(out))
    return out


def test_every_scenario_is_a_test_named_for_it_in_table_order(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, config: SweepConfig
) -> None:
    _sweep(pytester, monkeypatch, config, {"b": {"element": "#shot"}, "a": {"element": "#shot"}})

    result = pytester.runpytest("--collect-only", "-q", visual_sweep.__file__)

    result.stdout.fnmatch_lines(["*::test_scenario[[]b[]]", "*::test_scenario[[]a[]]"])


def test_an_empty_scenario_table_is_an_error_not_a_skip(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, config: SweepConfig
) -> None:
    _sweep(pytester, monkeypatch, config, {})

    result = pytester.runpytest("--collect-only", visual_sweep.__file__)

    assert result.ret != pytest.ExitCode.OK
    result.stdout.fnmatch_lines(["*the scenario table is empty*"])


def test_the_expected_font_family_the_macro_sets_gates_every_scenario(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, config: SweepConfig
) -> None:
    out = _sweep(pytester, monkeypatch, config, {"typeset": {"element": "#shot"}, "plain": {"element": "#shot"}})
    monkeypatch.setenv("EXPECTED_FONT_FAMILY", "Loaded Sans")

    result = pytester.runpytest(visual_sweep.__file__)

    # `plain` never sets type in the family, so for it the font is declared and not loaded.
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["*plain: Loaded Sans font did not load (unloaded)*"])
    assert list(_published(out)) == ["typeset-actual.png"]


def test_one_broken_scenario_does_not_hide_the_rest(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, config: SweepConfig
) -> None:
    out = _sweep(
        pytester,
        monkeypatch,
        config,
        {
            "plain": {"element": "#shot"},
            "throws": {"element": "#shot"},
            "late": {"element": "#shot", "readySelectors": [".arrived"]},
        },
    )

    result = pytester.runpytest(visual_sweep.__file__)

    result.assert_outcomes(passed=2, failed=1)
    result.stdout.fnmatch_lines(["*throws: uncaught page errors:*", "*Error: scene exploded*"])
    assert sorted(_published(out)) == ["late-actual.png", "plain-actual.png"]
    assert [asset.label for asset in _manifest(out).assets] == ["plain", "late"]


def test_the_output_suffix_comes_from_the_environment(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, config: SweepConfig
) -> None:
    out = _sweep(pytester, monkeypatch, config, {"plain": {"element": "#shot"}})
    monkeypatch.setenv("OUTPUT_SUFFIX", "")

    pytester.runpytest(visual_sweep.__file__).assert_outcomes(passed=1)

    assert list(_published(out)) == ["plain.png"]


if __name__ == "__main__":
    pytest_bazel.main()
