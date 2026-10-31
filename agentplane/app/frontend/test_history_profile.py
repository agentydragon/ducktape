"""Explicit Chromium CPU profiling of loaded history windows."""

import json

import pytest
import pytest_bazel
from playwright.async_api import expect

from agentplane.app.frontend.testing.app import IDLE_THREAD, UNNAMED_THREAD, AgentplaneFixture
from util.testing.viewports import Viewport
from util.testing.visual_capture import VisualPage

pytest_plugins = ("util.testing.visual_fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize("viewport", [Viewport(width=1280, height=900, device_scale_factor=1.0)], ids=["desktop"])
async def test_history_virtualization_cpu_profiles(view: VisualPage, app: AgentplaneFixture) -> None:
    page = view.page
    profile_summaries = []
    for row_count, thread_id in ((500, IDLE_THREAD), (2000, UNNAMED_THREAD)):
        await app.build_history_profile(row_count)
        session = await page.context.new_cdp_session(page)
        await session.send("Profiler.enable")
        await session.send("Profiler.setSamplingInterval", {"interval": 1000})
        await session.send("Profiler.start")
        profile_path = view.output_dir / f"history-{row_count}-rows.cpuprofile"
        try:
            await app.mount_thread(thread_id)
            history = page.get_by_role("region", name="Thread history", exact=True)
            head_row = history.locator('[data-history-row-key="item:profile-row-0"]')
            tail_row = history.locator(f'[data-history-row-key="item:profile-row-{row_count - 1}"]')
            await expect(history).to_have_attribute("data-layout-settled", "true")
            await expect(tail_row).to_be_attached()
            initial_count = await history.locator("[data-history-row-key]").count()
            assert 0 < initial_count <= 80, f"{row_count} rows mounted {initial_count} DOM rows at the tail"
            initial_top = await history.evaluate("element => element.scrollTop")
            assert initial_top > 0, f"{row_count} rows did not create scrollable history"

            await history.hover()
            for _ in range(row_count // 30 + 5):
                if await head_row.count():
                    break
                before = await history.evaluate(
                    "element => ({scrollHeight: element.scrollHeight, scrollTop: element.scrollTop})"
                )
                await page.mouse.wheel(0, -200_000)
                await page.wait_for_function(
                    """before => {
                      const history = document.querySelector('[aria-label="Thread history"]');
                      const head = history?.querySelector('[data-history-row-key="item:profile-row-0"]');
                      return !!head || (history !== null && (
                        history.scrollTop < before.scrollTop - 1 ||
                        history.scrollHeight > before.scrollHeight + 1
                      ));
                    }""",
                    arg=before,
                )
                await expect(history).to_have_attribute("data-layout-settled", "true")
            else:
                mounted_keys = await history.locator("[data-history-row-key]").evaluate_all(
                    "rows => rows.map(row => row.dataset.historyRowKey)"
                )
                raise AssertionError(
                    f"{row_count}-row profile never loaded its head after repeated upward scrolling; "
                    f"mounted {mounted_keys}"
                )
            await expect(head_row).to_be_attached()
            # Older-page prepends preserve the reader's anchor, so the head can mount in overscan
            # while scrollTop is still positive. Finish the actual trip to the top explicitly.
            await page.mouse.wheel(0, -200_000)
            await page.wait_for_function(
                "() => document.querySelector('[aria-label=\"Thread history\"]')?.scrollTop <= 1"
            )
            await expect(head_row).to_be_in_viewport()
            await expect(history).to_have_attribute("data-layout-settled", "true")
            top_count = await history.locator("[data-history-row-key]").count()
            assert 0 < top_count <= 80, f"{row_count} rows mounted {top_count} DOM rows at the top"

            await page.mouse.wheel(0, 200_000)
            await expect(tail_row).to_be_attached()
            await page.wait_for_function(
                "() => { const el = document.querySelector('[aria-label=\"Thread history\"]'); "
                "return el && el.scrollTop >= el.scrollHeight - el.clientHeight - 1; }"
            )
            await expect(history).to_have_attribute("data-layout-settled", "true")
            bottom_count = await history.locator("[data-history-row-key]").count()
            assert 0 < bottom_count <= 80, f"{row_count} rows mounted {bottom_count} DOM rows at the bottom"
        finally:
            response = await session.send("Profiler.stop")
            profile_path.write_text(json.dumps(response["profile"], separators=(",", ":")))
            await session.detach()

        profile = response["profile"]
        nodes = {node["id"]: node for node in profile["nodes"]}
        non_idle_sampled_time_us = sum(
            delta
            for node_id, delta in zip(profile.get("samples", []), profile.get("timeDeltas", []), strict=True)
            if nodes[node_id]["callFrame"].get("functionName") not in {"(idle)", "(program)"}
        )
        profile_summaries.append(
            {
                "row_count": row_count,
                "profile_path": str(profile_path),
                "sample_count": len(profile.get("samples", [])),
                "non_idle_sampled_time_us": non_idle_sampled_time_us,
                "mounted_row_counts": {"tail": initial_count, "top": top_count, "bottom": bottom_count},
            }
        )
        assert profile_summaries[-1]["sample_count"] > 0, f"Chromium returned an empty profile for {row_count} rows"

    (view.output_dir / "history-profile-summary.json").write_text(json.dumps(profile_summaries, indent=2))
    await view.check(context="history virtualization profiles complete")


if __name__ == "__main__":
    pytest_bazel.main()
