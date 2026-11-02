"""The capture API preserves health gates without owning the test's interactions."""

import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_bazel
from playwright.async_api import Playwright, expect

from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.testing.frontend_visual.capture import HarnessConfig, InlinePage, VisualHarness
from util.testing.frontend_visual.viewports import Viewport
from util.testing.pytest_outputs import BoundTestOutputs

# gazelle:include_dep //util:playwright
# gazelle:include_dep //util/testing/frontend_visual:fixtures
pytest_plugins = ("util.playwright", "util.testing.frontend_visual.fixtures")


def _records(output_dir: Path) -> list[dict[str, object]]:
    return [json.loads(path.read_text()) for path in (output_dir / "test-outputs" / "records").glob("*.json")]


def _record(output_dir: Path, *, role: str, checkpoint: str | None) -> dict[str, object]:
    return next(
        record for record in _records(output_dir) if record["role"] == role and record.get("checkpoint") == checkpoint
    )


def _payload(output_dir: Path, *, role: str, checkpoint: str | None) -> Path:
    return output_dir / "test-outputs" / str(_record(output_dir, role=role, checkpoint=checkpoint)["path"])


@pytest.fixture
def harness(playwright: Playwright, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case_id: str) -> VisualHarness:
    monkeypatch.setenv("VISUAL_TRACE", "0")
    bundle = tmp_path / "harness.js"
    bundle.write_text(
        "document.querySelector('button').onclick = () => document.querySelector('#shot').textContent = 'Opened';"
    )
    (tmp_path / "index.html").write_text(
        "<!doctype html><style>body { margin: 0 } #shot { width: 100px; height: 40px }</style>"
        "<div id='app'><button>Open</button><div id='shot'>Closed</div></div><script src='./harness.js'></script>"
    )
    return VisualHarness(
        playwright,
        HarnessConfig(harness_path=bundle, title="API test", expected_font_family=None),
        tmp_path / "out",
        case_id=case_id,
    )


async def test_test_owns_interaction_and_capture(harness: VisualHarness) -> None:
    async with harness.open() as view:
        assert await view.page.evaluate("location.search") == ""
        await expect(view.page.locator("#shot")).to_have_text("Closed")
        await view.page.get_by_role("button", name="Open").click()
        await expect(view.page.locator("#shot")).to_have_text("Opened")
        path = await view.capture(target=view.page.locator("#shot"), label="Opened panel")
    assert path.suffix == ".png"
    assert path == _payload(harness.output_dir, role="screenshot", checkpoint=None)
    assert _record(harness.output_dir, role="screenshot", checkpoint=None)["label"] == "Opened panel"


async def test_multiple_checkpoints_have_distinct_outputs(harness: VisualHarness) -> None:
    async with harness.open("plain") as view:
        first = await view.capture(target=view.page.locator("#shot"))
        await view.page.get_by_role("button", name="Open").click()
        second = await view.capture("opened", target=view.page.locator("#shot"))
        assert first.read_bytes() != second.read_bytes()
        with pytest.raises(FileExistsError):
            await view.capture()
        assert first.read_bytes() != second.read_bytes()


async def test_trace_default_opt_out_selection_and_failure_retention(
    harness: VisualHarness, monkeypatch: pytest.MonkeyPatch, case_id: str
) -> None:
    monkeypatch.delenv("VISUAL_TRACE", raising=False)
    monkeypatch.delenv("VISUAL_TRACE_CAPTURE_NAME", raising=False)
    async with harness.open("plain", trace_checkpoint="default"):
        pass
    assert zipfile.is_zipfile(_payload(harness.output_dir, role="trace", checkpoint="default"))

    monkeypatch.setenv("VISUAL_TRACE", "0")
    async with harness.open("plain", trace_checkpoint="disabled"):
        pass
    assert not any(r.get("checkpoint") == "disabled" for r in _records(harness.output_dir))

    monkeypatch.setenv("VISUAL_TRACE", "1")
    monkeypatch.setenv("VISUAL_TRACE_CAPTURE_NAME", "another/case.py::test_other")
    async with harness.open("plain", trace_checkpoint="unmatched"):
        pass
    assert not any(r.get("checkpoint") == "unmatched" for r in _records(harness.output_dir))

    monkeypatch.setenv("VISUAL_TRACE_CAPTURE_NAME", case_id)
    async with harness.open("plain", trace_checkpoint="selected") as view:
        await view.page.get_by_role("button", name="Open").click()
    assert zipfile.is_zipfile(_payload(harness.output_dir, role="trace", checkpoint="selected"))

    with pytest.raises(RuntimeError, match="trace this failure"):
        async with harness.open("plain", trace_checkpoint="failing"):
            raise RuntimeError("trace this failure")
    assert zipfile.is_zipfile(_payload(harness.output_dir, role="trace", checkpoint="failing"))

    monkeypatch.setenv("VISUAL_TRACE", "invalid")
    with pytest.raises(ValueError, match="VISUAL_TRACE must be 0 or 1"):
        async with harness.open("plain"):
            pass


async def test_capture_diagnostics_cover_viewport_and_crop_without_page_text(
    harness: VisualHarness, case_id: str
) -> None:
    async with harness.open("plain", viewport=Viewport(width=300, height=200, device_scale_factor=2)) as view:
        fixture = get_required_path(
            own_repo_rlocation("util/testing/frontend_visual/capture-diagnostics-fixture.js")
        ).read_text(encoding="utf-8")
        await view.page.evaluate(fixture)
        await view.capture("viewport", scale="css")
        await view.capture("cropped", target=view.page.locator(".entry"), scale="css")

    viewport = json.loads(_payload(harness.output_dir, role="diagnostics", checkpoint="viewport").read_text())
    assert viewport["case_id"] == case_id
    assert viewport["checkpoint"] == "viewport"
    assert viewport["sample"] == "before_screenshot"
    assert viewport["scale"] == "css"
    assert viewport["viewport"] == {"width": 300, "height": 200, "deviceScaleFactor": 2, "scrollX": 0, "scrollY": 50}
    assert viewport["focus"]["inputSelection"] == {"start": 2, "end": 9, "direction": "forward"}
    assert viewport["target"] is None
    assert {
        "rect": {"x": 110, "y": -30, "width": 80, "height": 40},
        "computedCssWidth": "80px",
        "computedCssHeight": "40px",
        "bitmapWidth": 160,
        "bitmapHeight": 80,
    } in viewport["canvases"]

    cropped = json.loads(_payload(harness.output_dir, role="diagnostics", checkpoint="cropped").read_text())
    assert cropped["checkpoint"] == "cropped"
    assert cropped["full_page"] is False
    assert cropped["target"]["rect"]["x"] == 10.25
    assert cropped["target"]["scrollHeight"] == 300
    history = cropped["targetScrollAncestors"][0]
    assert history["classes"] == ["history"]
    assert history["scrollTop"] == 17
    assert history["scrollHeight"] > history["clientHeight"]
    assert history["data-layout-settled"] == "true"
    assert history["data-scroll-mode"] == "virtualized"
    assert cropped["scrollables"][0]["data-scroll-mode"] == "virtualized"
    assert "sensitive-selection" not in json.dumps([viewport, cropped])
    assert {
        _record(harness.output_dir, role="screenshot", checkpoint=name)["label"] for name in ("viewport", "cropped")
    } == {"viewport", "cropped"}


async def test_unnamed_capture_uses_case_identity(harness: VisualHarness) -> None:
    async with harness.open() as view:
        image = await view.capture()
    assert image == _payload(harness.output_dir, role="screenshot", checkpoint=None)


async def test_ambiguous_crop_fails_before_publication(harness: VisualHarness) -> None:
    async with harness.open("plain") as view:
        with pytest.raises(ValueError, match="exactly one"):
            await view.capture("ambiguous", target=view.page.locator("div"))
    assert not harness.output_dir.exists()


async def test_no_capture_can_hide_a_page_error(harness: VisualHarness) -> None:
    async def crash() -> None:
        async with harness.open("plain") as view, view.page.expect_event("pageerror"):
            await view.page.add_script_tag(content="throw new Error('deliberate crash')")

    with pytest.raises(AssertionError, match="deliberate crash"):
        await crash()


async def test_external_request_fails_before_publication(harness: VisualHarness) -> None:
    async def escape() -> None:
        async with harness.open("plain") as view:
            await view.page.evaluate("() => fetch('https://escaped.test/').catch(() => {})")
            await view.capture("escaped")

    with pytest.raises(AssertionError, match="requests escaped"):
        await escape()
    assert not harness.output_dir.exists()


async def test_new_page_is_isolated_and_pixels_repeat(harness: VisualHarness) -> None:
    async with harness.open("plain", viewport=Viewport(width=300, height=200)) as view:
        first = await view.capture("first", target=view.page.locator("#shot"))
        await view.page.get_by_role("button", name="Open").click()
    async with harness.open("plain", viewport=Viewport(width=300, height=200)) as view:
        await expect(view.page.locator("#shot")).to_have_text("Closed")
        second = await view.capture("second", target=view.page.locator("#shot"))
    assert first.read_bytes() == second.read_bytes()


@pytest.fixture
def inline_harness(harness: VisualHarness, tmp_path: Path) -> VisualHarness:
    stylesheet = tmp_path / "inline.css"
    stylesheet.write_text(
        "@keyframes fade { to { opacity: 0 } } #shot { width: 100px; height: 40px; animation: fade 1s infinite }"
    )
    harness.config.harness_path.write_text(
        "document.querySelector('#app').innerHTML = '<div id=shot>Inline scene</div>';"
    )
    return VisualHarness(
        harness.playwright,
        replace(harness.config, inline_page=InlinePage(stylesheet_paths=(stylesheet,), base_href=None)),
        harness.output_dir,
        case_id=harness.case_id,
    )


async def test_inline_bootstrap_preserves_values_and_pins_animation(inline_harness: VisualHarness) -> None:
    value = "</script><p>Not markup</p>"
    async with inline_harness.open("inline", window_globals={"__VALUE__": value}) as view:
        assert await view.page.evaluate("window.__VALUE__") == value
        await view.check(context="inline")
        assert await view.page.locator("#shot").evaluate(
            "element => element.getAnimations().map(animation => animation.playState)"
        ) == ["paused"]
        first = await view.capture("first", target=view.page.locator("#shot"))
        second = await view.capture("second", target=view.page.locator("#shot"))
    assert first.read_bytes() == second.read_bytes()


async def test_inline_origin_and_mock_frame(inline_harness: VisualHarness, tmp_path: Path) -> None:
    document = tmp_path / "frame.html"
    document.write_text("<main>Mock frame</main>")
    inline_harness.config.harness_path.write_text(
        "localStorage.setItem('probe', 'present'); document.querySelector('#app').innerHTML = '<iframe src=\"https://frame.test/\"></iframe>';"
    )
    assert inline_harness.config.inline_page is not None
    harness = VisualHarness(
        inline_harness.playwright,
        replace(
            inline_harness.config,
            inline_page=replace(inline_harness.config.inline_page, url="https://app.test/"),
            served_documents={"https://frame.test/": document},
        ),
        inline_harness.output_dir,
        case_id=inline_harness.case_id,
    )
    async with harness.open("framed") as view:
        await expect(view.page.frame_locator("iframe").locator("main")).to_have_text("Mock frame")
        assert await view.page.evaluate("localStorage.getItem('probe')") == "present"
        await view.capture("framed")


async def test_undeclared_named_font_fails_before_publication(harness: VisualHarness) -> None:
    harness = VisualHarness(
        harness.playwright,
        replace(harness.config, expected_font_family="Absent Font"),
        harness.output_dir,
        case_id=harness.case_id,
    )
    async with harness.open("plain") as view:
        with pytest.raises(AssertionError, match=r"Absent Font font did not load.*undeclared"):
            await view.capture("missing-font")
    assert not harness.output_dir.exists()


async def test_capture_modes_are_mutually_exclusive(harness: VisualHarness) -> None:
    async with harness.open("plain") as view:
        with pytest.raises(ValueError, match="not both"):
            await view.capture("invalid", target=view.page.locator("#shot"), full_page=True)
    assert not harness.output_dir.exists()


@pytest.fixture(scope="session")
def isolated_output_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("visual-fixture-output")


@pytest.fixture
def bound_visual(
    request: pytest.FixtureRequest, tmp_path: Path, isolated_output_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[VisualHarness, BoundTestOutputs]:
    bundle = tmp_path / "harness.js"
    bundle.write_text("document.querySelector('#shot').textContent = 'Fixture scene';")
    (tmp_path / "index.html").write_text("<!doctype html><div id='shot'></div><script src='./harness.js'></script>")
    config = HarnessConfig(harness_path=bundle, title="Fixture test", expected_font_family=None)
    monkeypatch.setattr(HarnessConfig, "from_env", classmethod(lambda cls: config))
    monkeypatch.setenv("TEST_UNDECLARED_OUTPUTS_DIR", str(isolated_output_dir))
    test_outputs = request.getfixturevalue("test_outputs")
    return request.getfixturevalue("visual"), test_outputs


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("variant", ["left", "right"])
async def test_pytest_visual_binds_each_parameter(
    bound_visual: tuple[VisualHarness, BoundTestOutputs], monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    monkeypatch.setenv("VISUAL_TRACE", "1")
    monkeypatch.delenv("VISUAL_TRACE_CAPTURE_NAME", raising=False)
    visual, test_outputs = bound_visual
    case_id = test_outputs.case_id
    assert case_id.endswith(f"::test_pytest_visual_binds_each_parameter[{variant}]")
    assert visual.case_id == case_id
    assert visual.output_dir == test_outputs.output_dir

    for opening in ("first", "second"):
        async with visual.open(trace_checkpoint=f"trace-{opening}") as view:
            assert view.case_id == case_id
            await expect(view.page.locator("#shot")).to_have_text("Fixture scene")
            await view.capture(f"shot-{opening}")

    records = [
        record
        for record in _records(visual.output_dir)
        if "::test_pytest_visual_binds_each_parameter[" in str(record["case_id"])
    ]
    published_cases = {record["case_id"] for record in records}
    assert case_id in published_cases
    assert published_cases <= {case_id.replace(f"[{variant}]", "[left]"), case_id.replace(f"[{variant}]", "[right]")}
    expected_outputs = {
        ("diagnostics", "shot-first"),
        ("screenshot", "shot-first"),
        ("trace", "trace-first"),
        ("diagnostics", "shot-second"),
        ("screenshot", "shot-second"),
        ("trace", "trace-second"),
    }
    for published_case in published_cases:
        assert {
            (record["role"], record["checkpoint"]) for record in records if record["case_id"] == published_case
        } == expected_outputs
    assert len({record["path"] for record in records}) == len(records)
    for record in records:
        if record["role"] == "trace":
            assert zipfile.is_zipfile(visual.output_dir / "test-outputs" / str(record["path"]))


if __name__ == "__main__":
    pytest_bazel.main()
