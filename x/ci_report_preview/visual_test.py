from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import cast

import pytest
import pytest_bazel
from playwright.async_api import ConsoleMessage, Page, Playwright

from devinfra.bazel.runfiles import get_required_path, own_repo_rlocation
from devinfra.testing.output_writer import BoundTestOutputs
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.frontend_visual.capture import VisualPage
from util.testing.frontend_visual.context import FROZEN_NOW_MS, deterministic_browser_context, disable_animations_script
from util.testing.frontend_visual.page_capture import RequestFence, assert_network_settled

# gazelle:include_dep //util/testing/frontend_visual:fixtures
pytest_plugins = ("util.testing.frontend_visual.fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")

REPORTS = ("latency",)
VIEWPORTS = (("desktop", 1280, 900), ("mobile", 390, 844))
TARGET_SECTIONS = {
    "latency": (
        "followup",
        "capacity",
        "current-feedback",
        "bottlenecks",
        "selection",
        "interventions",
        "continuity",
        "method",
    )
}


def _console_error_handler(errors: list[str]) -> Callable[[ConsoleMessage], None]:
    def record(message: ConsoleMessage) -> None:
        if message.type == "error":
            errors.append(message.text)

    return record


async def _exercise_chart_controls(page: Page, visual: VisualPage) -> list[str]:
    exercised: list[str] = []
    selects = page.locator("select")
    for index in range(await selects.count()):
        select = selects.nth(index)
        if not await select.is_visible() or await select.is_disabled():
            continue
        states = await page.evaluate("window.__ciReportAudit.controls()")
        state = states["selects"][index]
        if not state["options"]:
            continue
        await select.select_option(value=state["options"][-1])
        await _assert_no_horizontal_overflow(page, context="selected last model option")
        await visual.check(context=f"changed chart selector {state['label']}")
        await select.select_option(value=state["value"])
        await visual.check(context=f"restored chart selector {state['label']}")
        exercised.append(f"select:{state['label']}")

    checkboxes = page.locator('input[type="checkbox"]')
    for index in range(await checkboxes.count()):
        checkbox = checkboxes.nth(index)
        if not await checkbox.is_visible() or await checkbox.is_disabled():
            continue
        state = (await page.evaluate("window.__ciReportAudit.controls()"))["checkboxes"][index]
        if state["checked"]:
            await checkbox.uncheck()
        else:
            await checkbox.check()
        await visual.check(context=f"changed report filter {state['label']}")
        if state["checked"]:
            await checkbox.check()
        else:
            await checkbox.uncheck()
        await visual.check(context=f"restored report filter {state['label']}")
        exercised.append(f"checkbox:{state['label']}")

    radios = page.locator('input[type="radio"]')
    radio_states = (await page.evaluate("window.__ciReportAudit.controls()"))["radios"]
    handled_groups: set[str] = set()
    for state in radio_states:
        group = state["name"] or f"unnamed-{state['index']}"
        if group in handled_groups or state["disabled"]:
            continue
        handled_groups.add(group)
        alternatives = [
            candidate
            for candidate in radio_states
            if candidate["name"] == state["name"] and not candidate["disabled"] and candidate["index"] != state["index"]
        ]
        if not alternatives:
            continue
        selected_index = next(
            (
                candidate["index"]
                for candidate in radio_states
                if candidate["name"] == state["name"] and candidate["checked"]
            ),
            state["index"],
        )
        alternative = alternatives[0]
        radio = radios.nth(alternative["index"])
        if not await radio.is_visible():
            continue
        await radio.check()
        await visual.check(context=f"changed report filter {state['label']}")
        await radios.nth(selected_index).check()
        await visual.check(context=f"restored report filter {state['label']}")
        exercised.append(f"radio:{state['label']}")
    return exercised


async def _assert_no_horizontal_overflow(page: Page, *, context: str) -> dict[str, int]:
    dimensions = cast(dict[str, int], await page.evaluate("window.__ciReportAudit.dimensions()"))
    assert dimensions["document_width"] <= dimensions["viewport_width"], (
        f"{context}: document overflows horizontally: {dimensions}"
    )
    return dimensions


async def _check_wide_table_scrolling(page: Page, *, viewport_name: str) -> list[dict[str, object]]:
    tables = cast(list[dict[str, object]], await page.evaluate("window.__ciReportAudit.wideTableScrollRegions()"))
    assert tables, "report has no inspectable wide tables"
    for table in tables:
        assert table["role"] == "region", f"wide table wrapper lacks region role: {table}"
        assert table["label"], f"wide table wrapper lacks accessible name: {table}"
        assert table["tab_index"] == 0, f"wide table wrapper is not keyboard focusable: {table}"

    if viewport_name == "mobile":
        for table in tables:
            scroll_width = cast(int, table["wrapper_scroll_width"])
            client_width = cast(int, table["wrapper_client_width"])
            assert scroll_width > client_width, f"wide table is compressed instead of horizontally scrollable: {table}"
            wrapper = page.locator(".table-scroll").nth(cast(int, table["wrapper_index"]))
            await wrapper.focus()
            assert await wrapper.evaluate("element => document.activeElement === element"), (
                f"wide table wrapper cannot receive keyboard focus: {table}"
            )
            await wrapper.press("ArrowRight")
            await page.wait_for_function(
                "index => document.querySelectorAll('.table-scroll')[index].scrollLeft > 0", arg=table["wrapper_index"]
            )
            scroll_left = await wrapper.evaluate("element => element.scrollLeft")
            assert scroll_left > 0, f"ArrowRight did not scroll the focused table: {table}"
            await _assert_no_horizontal_overflow(page, context="scrolling focused mobile table")
            await wrapper.evaluate("element => (element.scrollLeft = 0)")

    return tables


async def test_latency_report_renders_and_interacts(
    playwright_driver: Playwright, test_outputs: BoundTestOutputs
) -> None:
    output_dir = undeclared_outputs_dir()
    evidence: list[dict[str, object]] = []
    audit_script = get_required_path(own_repo_rlocation("x/ci_report_preview/report-audit.js")).read_text(
        encoding="utf-8"
    )

    for report_name in REPORTS:
        report_path = get_required_path(own_repo_rlocation(f"x/ci_report_preview/{report_name}.html"))
        report_bytes = report_path.read_bytes()
        report_sha256 = hashlib.sha256(report_bytes).hexdigest()

        for viewport_name, width, height in VIEWPORTS:
            async with deterministic_browser_context(
                playwright_driver, viewport={"width": width, "height": height}, frozen_now_ms=FROZEN_NOW_MS
            ) as context:
                await context.add_init_script(disable_animations_script())
                await context.add_init_script(audit_script)
                page = await context.new_page()
                console_errors: list[str] = []
                page.on("console", _console_error_handler(console_errors))
                with VisualPage(page, outputs=test_outputs, title="CI report preview") as visual:
                    fence = RequestFence(lambda request: request.url.startswith("file://"))
                    await fence.install(page)
                    await page.goto(report_path.absolute().as_uri(), wait_until="load")
                    await page.wait_for_function(
                        "document.readyState === 'complete' && document.body && document.body.innerText.trim().length > 0"
                    )
                    assert await page.title(), f"{report_name} has no document title"

                    anchors = await page.evaluate("window.__ciReportAudit.anchorAudit()")
                    assert not anchors["missing"], f"missing internal anchor targets: {anchors['missing']}"
                    link_count = anchors["link_count"]
                    details_count = await page.evaluate("window.__ciReportAudit.expandDetails()")
                    assert await page.locator("details[open]").count() == details_count

                    controls = await _exercise_chart_controls(page, visual)
                    wide_table_scroll_regions = await _check_wide_table_scrolling(page, viewport_name=viewport_name)
                    dimensions = await _assert_no_horizontal_overflow(
                        page, context=f"{report_name}/{viewport_name} with details open"
                    )
                    await assert_network_settled(page, context=f"{report_name}/{viewport_name}")
                    fence.assert_none_escaped(context=f"{report_name}/{viewport_name}")
                    assert not console_errors, f"{report_name}/{viewport_name} console errors: {console_errors}"

                    captures: list[dict[str, str]] = []
                    await page.evaluate("window.scrollTo({ top: 0, left: 0, behavior: 'instant' })")
                    await page.wait_for_function("window.scrollY === 0")
                    first_screen = await visual.capture(
                        f"{report_name}-{viewport_name}-first-screen",
                        animations="disabled",
                        label=f"{report_name} report, {viewport_name}, first screen",
                    )
                    captures.append(
                        {"name": first_screen.name, "sha256": hashlib.sha256(first_screen.read_bytes()).hexdigest()}
                    )
                    for section_id in TARGET_SECTIONS[report_name]:
                        target = page.locator(f"#{section_id}")
                        assert await target.count() == 1, f"{report_name} is missing section #{section_id}"
                        await target.scroll_into_view_if_needed()
                        section_capture = await visual.capture(
                            f"{report_name}-{viewport_name}-{section_id}",
                            target=target,
                            animations="disabled",
                            label=f"{report_name} report, {viewport_name}, #{section_id}",
                        )
                        captures.append(
                            {
                                "name": section_capture.name,
                                "sha256": hashlib.sha256(section_capture.read_bytes()).hexdigest(),
                            }
                        )

                    assert not console_errors, f"{report_name}/{viewport_name} console errors: {console_errors}"

                    evidence.append(
                        {
                            "report": report_name,
                            "report_sha256": report_sha256,
                            "viewport": {"name": viewport_name, "width": width, "height": height},
                            "internal_link_count": link_count,
                            "details_expanded": details_count,
                            "interactive_controls_exercised": controls,
                            "wide_table_scroll_regions": wide_table_scroll_regions,
                            "document_dimensions_with_details_open": dimensions,
                            "captures": captures,
                        }
                    )

    (output_dir / "preview-evidence.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    pytest_bazel.main()
