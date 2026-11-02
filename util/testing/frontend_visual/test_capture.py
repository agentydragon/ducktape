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

# gazelle:include_dep //util:playwright
pytest_plugins = ("util.playwright",)


def _records(output_dir: Path) -> list[dict[str, object]]:
    return [json.loads(path.read_text()) for path in (output_dir / "test-outputs" / "records").glob("*.json")]


def _record(output_dir: Path, *, role: str, checkpoint: str | None) -> dict[str, object]:
    return next(
        record for record in _records(output_dir) if record["role"] == role and record.get("checkpoint") == checkpoint
    )


def _payload(output_dir: Path, *, role: str, checkpoint: str | None) -> Path:
    return output_dir / "test-outputs" / str(_record(output_dir, role=role, checkpoint=checkpoint)["path"])


@pytest.fixture
def harness(playwright: Playwright, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> VisualHarness:
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
        playwright, HarnessConfig(harness_path=bundle, title="API test", expected_font_family=None), tmp_path / "out"
    )


async def test_test_owns_interaction_and_capture(harness: VisualHarness, case_id: str) -> None:
    async with harness.open(case_id=case_id) as view:
        assert await view.page.evaluate("location.search") == ""
        await expect(view.page.locator("#shot")).to_have_text("Closed")
        await view.page.get_by_role("button", name="Open").click()
        await expect(view.page.locator("#shot")).to_have_text("Opened")
        path = await view.capture(target=view.page.locator("#shot"), label="Opened panel")
    assert path.suffix == ".png"
    assert path == _payload(harness.output_dir, role="screenshot", checkpoint=None)
    assert _record(harness.output_dir, role="screenshot", checkpoint=None)["label"] == "Opened panel"


async def test_multiple_checkpoints_have_distinct_outputs(harness: VisualHarness, case_id: str) -> None:
    async with harness.open("plain", case_id=case_id) as view:
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
    async with harness.open("plain", case_id=case_id, trace_checkpoint="default"):
        pass
    assert zipfile.is_zipfile(_payload(harness.output_dir, role="trace", checkpoint="default"))

    monkeypatch.setenv("VISUAL_TRACE", "0")
    async with harness.open("plain", case_id=case_id, trace_checkpoint="disabled"):
        pass
    assert not any(r.get("checkpoint") == "disabled" for r in _records(harness.output_dir))

    monkeypatch.setenv("VISUAL_TRACE", "1")
    monkeypatch.setenv("VISUAL_TRACE_CAPTURE_NAME", "another/case.py::test_other")
    async with harness.open("plain", case_id=case_id, trace_checkpoint="unmatched"):
        pass
    assert not any(r.get("checkpoint") == "unmatched" for r in _records(harness.output_dir))

    monkeypatch.setenv("VISUAL_TRACE_CAPTURE_NAME", case_id)
    async with harness.open("plain", case_id=case_id, trace_checkpoint="selected") as view:
        await view.page.get_by_role("button", name="Open").click()
    assert zipfile.is_zipfile(_payload(harness.output_dir, role="trace", checkpoint="selected"))

    with pytest.raises(RuntimeError, match="trace this failure"):
        async with harness.open("plain", case_id=case_id, trace_checkpoint="failing"):
            raise RuntimeError("trace this failure")
    assert zipfile.is_zipfile(_payload(harness.output_dir, role="trace", checkpoint="failing"))

    monkeypatch.setenv("VISUAL_TRACE", "invalid")
    with pytest.raises(ValueError, match="VISUAL_TRACE must be 0 or 1"):
        async with harness.open("plain", case_id=case_id):
            pass


async def test_capture_diagnostics_cover_viewport_and_crop_without_page_text(
    harness: VisualHarness, case_id: str
) -> None:
    async with harness.open(
        "plain", viewport=Viewport(width=300, height=200, device_scale_factor=2), case_id=case_id
    ) as view:
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


async def test_unnamed_capture_uses_case_identity(harness: VisualHarness, case_id: str) -> None:
    async with harness.open(case_id=case_id) as view:
        image = await view.capture()
    assert image == _payload(harness.output_dir, role="screenshot", checkpoint=None)


async def test_ambiguous_crop_fails_before_publication(harness: VisualHarness, case_id: str) -> None:
    async with harness.open("plain", case_id=case_id) as view:
        with pytest.raises(ValueError, match="exactly one"):
            await view.capture("ambiguous", target=view.page.locator("div"))
    assert not harness.output_dir.exists()


async def test_no_capture_can_hide_a_page_error(harness: VisualHarness, case_id: str) -> None:
    async def crash() -> None:
        async with harness.open("plain", case_id=case_id) as view, view.page.expect_event("pageerror"):
            await view.page.add_script_tag(content="throw new Error('deliberate crash')")

    with pytest.raises(AssertionError, match="deliberate crash"):
        await crash()


async def test_external_request_fails_before_publication(harness: VisualHarness, case_id: str) -> None:
    async def escape() -> None:
        async with harness.open("plain", case_id=case_id) as view:
            await view.page.evaluate("() => fetch('https://escaped.test/').catch(() => {})")
            await view.capture("escaped")

    with pytest.raises(AssertionError, match="requests escaped"):
        await escape()
    assert not harness.output_dir.exists()


async def test_new_page_is_isolated_and_pixels_repeat(harness: VisualHarness, case_id: str) -> None:
    async with harness.open("plain", viewport=Viewport(width=300, height=200), case_id=case_id) as view:
        first = await view.capture("first", target=view.page.locator("#shot"))
        await view.page.get_by_role("button", name="Open").click()
    async with harness.open("plain", viewport=Viewport(width=300, height=200), case_id=case_id) as view:
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
    )


async def test_inline_bootstrap_preserves_values_and_pins_animation(
    inline_harness: VisualHarness, case_id: str
) -> None:
    value = "</script><p>Not markup</p>"
    async with inline_harness.open("inline", window_globals={"__VALUE__": value}, case_id=case_id) as view:
        assert await view.page.evaluate("window.__VALUE__") == value
        await view.check(context="inline")
        assert await view.page.locator("#shot").evaluate(
            "element => element.getAnimations().map(animation => animation.playState)"
        ) == ["paused"]
        first = await view.capture("first", target=view.page.locator("#shot"))
        second = await view.capture("second", target=view.page.locator("#shot"))
    assert first.read_bytes() == second.read_bytes()


async def test_inline_origin_and_mock_frame(inline_harness: VisualHarness, tmp_path: Path, case_id: str) -> None:
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
    )
    async with harness.open("framed", case_id=case_id) as view:
        await expect(view.page.frame_locator("iframe").locator("main")).to_have_text("Mock frame")
        assert await view.page.evaluate("localStorage.getItem('probe')") == "present"
        await view.capture("framed")


async def test_undeclared_named_font_fails_before_publication(harness: VisualHarness, case_id: str) -> None:
    harness = VisualHarness(
        harness.playwright, replace(harness.config, expected_font_family="Absent Font"), harness.output_dir
    )
    async with harness.open("plain", case_id=case_id) as view:
        with pytest.raises(AssertionError, match=r"Absent Font font did not load.*undeclared"):
            await view.capture("missing-font")
    assert not harness.output_dir.exists()


async def test_capture_modes_are_mutually_exclusive(harness: VisualHarness, case_id: str) -> None:
    async with harness.open("plain", case_id=case_id) as view:
        with pytest.raises(ValueError, match="not both"):
            await view.capture("invalid", target=view.page.locator("#shot"), full_page=True)
    assert not harness.output_dir.exists()


if __name__ == "__main__":
    pytest_bazel.main()
