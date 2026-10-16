"""Exercise responsive mixed-chart layout and pixels in deterministic Chromium."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator

import pytest
import pytest_bazel
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import Playwright
from pydantic import BaseModel, JsonValue, TypeAdapter

from util.bazel.runfiles import get_required_path
from util.testing.asgi import serve_app_sync
from util.testing.frontend_visual import deterministic_browser_context
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.visual_capture import playwright_trace

# gazelle:include_dep //util:playwright
pytest_plugins = ("util.playwright",)

_BUNDLE_DIR = get_required_path("_main/third_party/chartjs/layout-dist/layout-fixture.js").parent
_HTML = """<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>Chart.js layout fixture</title></head>
  <body style="margin: 0">
    <div id="host" style="height: 250px"><canvas id="chart"></canvas></div>
    <script type="module" src="/layout-fixture.js"></script>
  </body>
</html>
"""


class CanvasSnapshot(BaseModel):
    png: str
    geometry: JsonValue


class Checkpoint(BaseModel):
    stage: str
    png: str
    sha256: str
    geometry: JsonValue


@pytest.fixture(scope="module")
def layout_fixture_server() -> Iterator[str]:
    app = FastAPI()

    @app.get("/")
    def index() -> HTMLResponse:
        return HTMLResponse(_HTML)

    app.mount("/", StaticFiles(directory=str(_BUNDLE_DIR)), name="layout-fixture")
    with serve_app_sync(app) as base_url:
        yield base_url


@pytest.mark.parametrize(
    ("device_scale_factor", "host_width"),
    [
        pytest.param(1, 292, id="dpr1-width292"),
        pytest.param(2, 292, id="dpr2-width292"),
        pytest.param(1, 222, id="dpr1-width222"),
        pytest.param(2, 222, id="dpr2-width222"),
    ],
)
async def test_chart_layout_is_repeatable_across_updates_and_resize(
    playwright: Playwright, layout_fixture_server: str, device_scale_factor: float, host_width: int
) -> None:
    case_name = f"dpr{device_scale_factor}-width{host_width}"
    output_dir = undeclared_outputs_dir()
    evidence_path = output_dir / f"chartjs-layout-{case_name}.json"
    checkpoints: list[Checkpoint] = []
    page_errors: list[str] = []

    async with (
        deterministic_browser_context(
            playwright, viewport={"width": 600, "height": 400}, frozen_now_ms=0, device_scale_factor=device_scale_factor
        ) as context,
        playwright_trace(context, output_dir, f"chartjs-layout-{case_name}"),
    ):
        page = await context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        await page.goto(f"{layout_fixture_server}/?width={host_width}", wait_until="load")
        await page.wait_for_function(
            "state => window.chartProbe?.hasDrawnAfter(state)", arg={"previousCount": 0, "expectedWidth": host_width}
        )

        async def checkpoint(stage: str) -> Checkpoint:
            snapshot = CanvasSnapshot.model_validate(await page.evaluate("() => window.chartProbe.snapshot()"))
            data_url = snapshot.png
            assert data_url.startswith("data:image/png;base64,")
            png = base64.b64decode(data_url.partition(",")[2])
            filename = f"chartjs-layout-{case_name}-{stage}.png"
            (output_dir / filename).write_bytes(png)
            record = Checkpoint(
                stage=stage, png=filename, sha256=hashlib.sha256(png).hexdigest(), geometry=snapshot.geometry
            )
            checkpoints.append(record)
            evidence_path.write_bytes(TypeAdapter(list[Checkpoint]).dump_json(checkpoints, indent=2) + b"\n")
            return record

        async def wait_for_draw(previous_count: int, expected_width: int | None = None) -> None:
            await page.wait_for_function(
                "state => window.chartProbe.hasDrawnAfter(state)",
                arg={"previousCount": previous_count, "expectedWidth": expected_width},
            )

        async def change(
            stage: str, operation: str, argument: JsonValue = None, *, expected_width: int | None = None
        ) -> Checkpoint:
            previous_count = TypeAdapter(int).validate_python(await page.evaluate("() => window.chartProbe.drawCount"))
            await page.evaluate(operation, argument)
            await wait_for_draw(previous_count, expected_width)
            return await checkpoint(stage)

        def assert_same(first: Checkpoint, second: Checkpoint, stage: str) -> None:
            assert first.sha256 == second.sha256, (
                f"{case_name} {stage}: canvas pixels changed "
                f"({first.sha256} -> {second.sha256}); details: {evidence_path}"
            )
            assert first.geometry == second.geometry, (
                f"{case_name} {stage}: chart/axis geometry changed; details: {evidence_path}"
            )

        initial = await checkpoint("initial")
        after_update = await change("after-update", "() => window.chartProbe.update()")
        assert_same(initial, after_update, "explicit no-op update")

        widened = await change(
            "widened",
            "width => window.chartProbe.setHostWidth(width)",
            host_width + 100,
            expected_width=host_width + 100,
        )
        assert widened.sha256 != initial.sha256, f"{case_name} resize did not change canvas pixels"

        restored_width = await change(
            "restored-width", "width => window.chartProbe.setHostWidth(width)", host_width, expected_width=host_width
        )
        assert_same(initial, restored_width, "restore after ResizeObserver resize")

        direct_active = await change("direct-active", "() => window.chartProbe.update('active')")
        assert direct_active.sha256 != initial.sha256, f"{case_name} active update did not change canvas pixels"
        repeated_direct_active = await change("direct-active-repeat", "() => window.chartProbe.update('active')")
        assert_same(direct_active, repeated_direct_active, "repeated explicit active-mode update")
        restored_mode = await change("restored-mode", "() => window.chartProbe.update()")
        assert_same(initial, restored_mode, "restore after explicit active-mode update")

        function_active = await change("function-active", "() => window.chartProbe.updateWithModeFunction()")
        assert await page.evaluate("() => window.chartProbe.modeFunctionCalledOncePerDataset()"), (
            f"{case_name} update-mode callback must run once per dataset"
        )
        assert function_active.sha256 != initial.sha256, f"{case_name} function-valued mode did not change pixels"
        function_repeat = await change("function-active-repeat", "() => window.chartProbe.updateWithModeFunction()")
        assert_same(function_active, function_repeat, "repeated function-valued active mode")
        function_restored = await change("function-restored", "() => window.chartProbe.update()")
        assert_same(initial, function_restored, "restore after function-valued update mode")

        active = await change("dataset-active", "() => window.chartProbe.setDatasetActive(true)")
        assert active.sha256 != initial.sha256, f"{case_name} active dataset style did not change canvas pixels"

        assert direct_active.geometry == active.geometry, f"{case_name} explicit active mode used default overflow"
        assert function_active.geometry == active.geometry, f"{case_name} function-valued mode used default overflow"

        repeated_active = await change("dataset-active-repeat", "() => window.chartProbe.update('active')")
        assert_same(active, repeated_active, "repeated active-state update")

        restored_active = await change("dataset-inactive", "() => window.chartProbe.setDatasetActive(false)")
        assert_same(initial, restored_active, "restore after dataset hover")

        changed_data = await change("changed-data", "() => window.chartProbe.setBarValue(3, 25)")
        assert changed_data.sha256 != initial.sha256, f"{case_name} data change did not change canvas pixels"

        restored_data = await change("restored-data", "() => window.chartProbe.setBarValue(3, 10)")
        assert_same(initial, restored_data, "restore after data change")

        changed_border = await change("border-width-4", "() => window.chartProbe.setLineBorderWidth(4)")
        assert changed_border.sha256 != initial.sha256, f"{case_name} line-width change did not change canvas pixels"

        restored_border = await change("restored-border-width", "() => window.chartProbe.setLineBorderWidth(2)")
        assert_same(initial, restored_border, "restore after line-width change")

        assert not page_errors, f"{case_name} browser errors: {page_errors}"


if __name__ == "__main__":
    pytest_bazel.main()
