"""The capture API preserves health gates without owning the test's interactions."""

import asyncio
import json
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
import pytest_bazel
from playwright.async_api import BrowserContext, Page, Playwright, expect
from python.runfiles import Runfiles

from devinfra.testing.output_records import output_key
from devinfra.testing.output_writer import BoundTestOutputs
from util.testing.frontend_visual.capture import (
    HarnessConfig,
    InlinePage,
    VisualHarness,
    VisualPage,
    open_visual_page,
    playwright_trace,
)
from util.testing.frontend_visual.viewports import Viewport

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


async def _crash_visual_page(page: Page, outputs: BoundTestOutputs, *, fail_test: bool = False) -> None:
    with VisualPage(page, outputs=outputs, title="Example"):
        async with page.expect_event("pageerror"):
            await page.evaluate("() => setTimeout(() => { throw new Error('browser crash') }, 0)")
        if fail_test:
            raise ValueError("original failure")


async def test_visual_page_checks_errors_after_last_capture(page: Page, tmp_path: Path) -> None:
    outputs = BoundTestOutputs(tmp_path, "example::test")
    with pytest.raises(AssertionError, match="browser crash"):
        await _crash_visual_page(page, outputs)


async def test_visual_page_preserves_existing_failure(page: Page, tmp_path: Path) -> None:
    outputs = BoundTestOutputs(tmp_path, "example::test")
    with pytest.raises(ValueError, match="original failure") as caught:
        await _crash_visual_page(page, outputs, fail_test=True)
    assert any("browser crash" in note for note in getattr(caught.value, "__notes__", []))


async def test_open_visual_page_monitors_navigation_and_closes_page(page: Page, tmp_path: Path) -> None:
    outputs = BoundTestOutputs(tmp_path, "example::test")
    captured_page: Page | None = None

    async def navigate_to_crash() -> None:
        nonlocal captured_page
        async with open_visual_page(page.context, outputs, title="Example") as view:
            captured_page = view.page
            async with view.page.expect_event("pageerror"):
                await view.page.goto("data:text/html,<script>throw new Error('load crash')</script>")

    with pytest.raises(AssertionError, match="load crash"):
        await navigate_to_crash()
    assert captured_page is not None
    assert captured_page.is_closed()


@pytest.fixture
def harness(
    playwright: Playwright, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test_outputs: BoundTestOutputs
) -> VisualHarness:
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
        BoundTestOutputs(tmp_path / "out", test_outputs.case_id),
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


async def test_harness_passes_explicit_query_to_the_document(harness: VisualHarness) -> None:
    async with harness.open(query={"page": "scene & detail"}) as view:
        assert await view.page.evaluate("new URLSearchParams(location.search).get('page')") == "scene & detail"


async def test_page_outputs_publish_additional_role_under_capture_case(harness: VisualHarness) -> None:
    async with harness.open() as view:
        assert view.outputs.output_dir == view.output_dir
        assert view.outputs.case_id == view.case_id
        extra = view.outputs.publish(
            checkpoint="layout", role="geometry", data='{"width":100}', suffix="json", label="Layout geometry"
        )
        screenshot = await view.capture("layout")
        with pytest.raises(FileExistsError):
            view.outputs.publish(checkpoint="layout", role="geometry", data="duplicate", suffix="json")

    assert extra.read_text() == '{"width":100}'
    assert screenshot == _payload(harness.output_dir, role="screenshot", checkpoint="layout")
    assert _payload(harness.output_dir, role="diagnostics", checkpoint="layout").is_file()
    assert {(record["case_id"], record["checkpoint"], record["role"]) for record in _records(harness.output_dir)} == {
        (harness.case_id, "layout", "geometry"),
        (harness.case_id, "layout", "diagnostics"),
        (harness.case_id, "layout", "screenshot"),
    }
    assert _record(harness.output_dir, role="geometry", checkpoint="layout")["label"] == "Layout geometry"


async def test_multiple_checkpoints_have_distinct_outputs(harness: VisualHarness) -> None:
    async with harness.open() as view:
        first = await view.capture(target=view.page.locator("#shot"))
        await view.page.get_by_role("button", name="Open").click()
        second = await view.capture("opened", target=view.page.locator("#shot"))
        assert first.read_bytes() != second.read_bytes()
        with pytest.raises(FileExistsError):
            await view.capture()
        assert first.read_bytes() != second.read_bytes()


async def test_trace_default_opt_out_selection_and_failure_retention(
    harness: VisualHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VISUAL_TRACE", raising=False)
    monkeypatch.delenv("VISUAL_TRACE_CAPTURE_NAME", raising=False)
    async with harness.open(trace_checkpoint="default"):
        pass
    assert zipfile.is_zipfile(_payload(harness.output_dir, role="trace", checkpoint="default"))

    monkeypatch.setenv("VISUAL_TRACE", "0")
    async with harness.open(trace_checkpoint="disabled"):
        pass
    assert not any(r.get("checkpoint") == "disabled" for r in _records(harness.output_dir))

    monkeypatch.setenv("VISUAL_TRACE", "1")
    monkeypatch.setenv("VISUAL_TRACE_CAPTURE_NAME", "another/case.py::test_other")
    async with harness.open(trace_checkpoint="unmatched"):
        pass
    assert not any(r.get("checkpoint") == "unmatched" for r in _records(harness.output_dir))

    monkeypatch.setenv("VISUAL_TRACE_CAPTURE_NAME", harness.case_id)
    async with harness.open(trace_checkpoint="selected") as view:
        await view.page.get_by_role("button", name="Open").click()
    assert zipfile.is_zipfile(_payload(harness.output_dir, role="trace", checkpoint="selected"))

    with pytest.raises(RuntimeError, match="trace this failure"):
        async with harness.open(trace_checkpoint="failing"):
            raise RuntimeError("trace this failure")
    assert zipfile.is_zipfile(_payload(harness.output_dir, role="trace", checkpoint="failing"))

    monkeypatch.setenv("VISUAL_TRACE", "invalid")
    with pytest.raises(ValueError, match="VISUAL_TRACE must be 0 or 1"):
        async with harness.open():
            pass


async def test_duplicate_trace_keeps_first_record(harness: VisualHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISUAL_TRACE", "1")
    async with harness.open(trace_checkpoint="repeated"):
        pass
    first_trace = _payload(harness.output_dir, role="trace", checkpoint="repeated")
    original = first_trace.read_bytes()

    with pytest.raises(FileExistsError, match="identity already claimed"):
        async with harness.open(trace_checkpoint="repeated"):
            pass

    assert first_trace.read_bytes() == original
    assert zipfile.is_zipfile(first_trace)
    assert len(_records(harness.output_dir)) == 1
    assert not list(harness.output_dir.glob("trace-recovery-*"))


async def test_failed_case_retains_raw_trace_if_identity_is_claimed(
    harness: VisualHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VISUAL_TRACE", "1")
    async with harness.open(trace_checkpoint="repeated"):
        pass
    first_trace = _payload(harness.output_dir, role="trace", checkpoint="repeated")
    original = first_trace.read_bytes()

    with pytest.raises(RuntimeError, match="original failure") as caught:
        async with harness.open(trace_checkpoint="repeated"):
            raise RuntimeError("original failure")

    assert first_trace.read_bytes() == original
    assert len(_records(harness.output_dir)) == 1
    notes = getattr(caught.value, "__notes__", [])
    assert len(notes) == 1
    assert "FileExistsError" in notes[0]
    raw_trace = Path(notes[0].split("raw trace retained at ", maxsplit=1)[1])
    assert zipfile.is_zipfile(raw_trace)
    assert raw_trace.parent.parent == harness.output_dir


async def test_concurrent_traces_claim_identity_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISUAL_TRACE", "1")
    monkeypatch.delenv("VISUAL_TRACE_CAPTURE_NAME", raising=False)
    outputs = BoundTestOutputs(tmp_path / "out", "visual/test.py::test_same_case")
    both_stopped = asyncio.Event()
    staging_paths: list[Path] = []

    class FakeTracing:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        async def start(self, **options: object) -> None:
            assert options == {
                "name": f"visual capture: {outputs.case_id}",
                "screenshots": True,
                "snapshots": True,
                "sources": False,
            }

        async def stop(self, *, path: Path) -> None:
            staging_paths.append(path)
            if len(staging_paths) == 2:
                both_stopped.set()
            await both_stopped.wait()
            await asyncio.to_thread(path.write_bytes, self.payload)

    class FakeContext:
        def __init__(self, payload: bytes) -> None:
            self.tracing = FakeTracing(payload)

    async def write_trace(payload: bytes) -> None:
        async with playwright_trace(cast(BrowserContext, FakeContext(payload)), outputs, "shared"):
            pass

    async with asyncio.timeout(10):
        results = await asyncio.gather(
            write_trace(b"first trace"), write_trace(b"second trace"), return_exceptions=True
        )
    failures = [result for result in results if isinstance(result, BaseException)]
    assert len(failures) == 1
    assert isinstance(failures[0], FileExistsError)
    assert len(staging_paths) == 2
    assert staging_paths[0] != staging_paths[1]
    assert all(not path.is_relative_to(outputs.output_dir) for path in staging_paths)
    assert all(not path.exists() for path in staging_paths)
    assert not list(outputs.output_dir.glob("trace-recovery-*"))
    assert _records(outputs.output_dir) == [
        {
            "schema": "ducktape.test-output.v1",
            "case_id": outputs.case_id,
            "checkpoint": "shared",
            "role": "trace",
            "path": f"files/{output_key(outputs.case_id, 'shared', 'trace')}.zip",
        }
    ]
    assert _payload(outputs.output_dir, role="trace", checkpoint="shared").read_bytes() in {
        b"first trace",
        b"second trace",
    }


async def test_capture_diagnostics_cover_viewport_and_crop_without_page_text(harness: VisualHarness) -> None:
    async with harness.open(viewport=Viewport(width=300, height=200, device_scale_factor=2)) as view:
        fixture = (
            Runfiles.CreateOrRaise().root()
            / "ducktape/util/testing/frontend_visual/assets/capture_diagnostics_fixture.js"
        ).read_text(encoding="utf-8")
        await view.page.evaluate(fixture)
        await view.capture("viewport", scale="css")
        await view.capture("cropped", target=view.page.locator(".entry"), scale="css")

    viewport = json.loads(_payload(harness.output_dir, role="diagnostics", checkpoint="viewport").read_text())
    assert viewport["case_id"] == harness.case_id
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
    async with harness.open() as view:
        with pytest.raises(ValueError, match="exactly one"):
            await view.capture("ambiguous", target=view.page.locator("div"))
    assert not harness.output_dir.exists()


async def test_no_capture_can_hide_a_page_error(harness: VisualHarness) -> None:
    async def crash() -> None:
        async with harness.open() as view, view.page.expect_event("pageerror"):
            await view.page.add_script_tag(content="throw new Error('deliberate crash')")

    with pytest.raises(AssertionError, match="deliberate crash"):
        await crash()


async def test_harness_preserves_existing_failure_and_attaches_browser_errors(harness: VisualHarness) -> None:
    async def crash() -> None:
        async with harness.open() as view:
            async with view.page.expect_event("pageerror"):
                await view.page.evaluate("() => setTimeout(() => { throw new Error('browser crash') }, 0)")
            raise ValueError("original failure")

    with pytest.raises(ValueError, match="original failure") as caught:
        await crash()
    assert any("browser crash" in note for note in getattr(caught.value, "__notes__", []))


async def test_external_request_fails_before_publication(harness: VisualHarness) -> None:
    async def escape() -> None:
        async with harness.open() as view:
            await view.page.evaluate("() => fetch('https://escaped.test/').catch(() => {})")
            await view.capture("escaped")

    with pytest.raises(AssertionError, match="requests escaped"):
        await escape()
    assert not harness.output_dir.exists()


async def test_new_page_is_isolated_and_pixels_repeat(harness: VisualHarness) -> None:
    async with harness.open(viewport=Viewport(width=300, height=200)) as view:
        first = await view.capture("first", target=view.page.locator("#shot"))
        await view.page.get_by_role("button", name="Open").click()
    async with harness.open(viewport=Viewport(width=300, height=200)) as view:
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
        harness.outputs,
    )


async def test_inline_bootstrap_preserves_values_and_pins_animation(inline_harness: VisualHarness) -> None:
    value = "</script><p>Not markup</p>"
    async with inline_harness.open(window_globals={"__VALUE__": value}) as view:
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
        inline_harness.outputs,
    )
    async with harness.open() as view:
        await expect(view.page.frame_locator("iframe").locator("main")).to_have_text("Mock frame")
        assert await view.page.evaluate("localStorage.getItem('probe')") == "present"
        await view.capture("framed")


async def test_undeclared_named_font_fails_before_publication(harness: VisualHarness) -> None:
    harness = VisualHarness(
        harness.playwright, replace(harness.config, expected_font_family="Absent Font"), harness.outputs
    )
    async with harness.open() as view:
        with pytest.raises(AssertionError, match=r"Absent Font font did not load.*undeclared"):
            await view.capture("missing-font")
    assert not harness.output_dir.exists()


async def test_capture_modes_are_mutually_exclusive(harness: VisualHarness) -> None:
    async with harness.open() as view:
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
    test_outputs = cast(BoundTestOutputs, request.getfixturevalue("test_outputs"))
    return cast(VisualHarness, request.getfixturevalue("visual")), test_outputs


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
