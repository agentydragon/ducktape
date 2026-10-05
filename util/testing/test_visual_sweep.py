import json
import struct
import textwrap
from pathlib import Path

import pytest
import pytest_bazel
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

# What each scene of the test harness page shows, by `?page=` name. The scenes that fail do so in
# the way their name says; `index.html` and this file are all the harness there is.
_HARNESS_JS = """
const scene = new URLSearchParams(location.search).get("page");
const app = document.getElementById("app");
const shot = '<div id="shot"><div id="target"></div></div>';
if (scene === "plain") app.innerHTML = shot;
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
    return SweepConfig(
        harness_path=harness / "harness.js",
        scenarios_path=tmp_path / "scenarios.json",
        title="Test sweep",
        expected_font_family=None,
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


async def test_a_declared_font_that_did_not_load_fails_the_scenario(
    playwright: Playwright, config: SweepConfig, tmp_path: Path
) -> None:
    declared = SweepConfig(
        harness_path=config.harness_path,
        scenarios_path=config.scenarios_path,
        title=config.title,
        expected_font_family="Declared Sans",
    )

    with pytest.raises(AssertionError, match="plain: Declared Sans font did not load"):
        await capture_scenario(playwright, "plain", Scenario(element="#shot"), config=declared, output_dir=tmp_path)


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


if __name__ == "__main__":
    pytest_bazel.main()
