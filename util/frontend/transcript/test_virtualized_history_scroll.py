from itertools import pairwise

import pytest
import pytest_bazel
from playwright.async_api import Locator, expect

from util.testing.frontend_visual.capture import VisualHarness
from util.testing.frontend_visual.viewports import Viewport

pytest_plugins = ("util.testing.frontend_visual.fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _wait_for_settled_layout(history: Locator) -> None:
    await expect(history).to_have_attribute("data-layout-settled", "true")
    await history.evaluate(
        """async element => {
          let previousHeight = element.scrollHeight;
          let stableFrames = 0;
          for (let frame = 0; frame < 120; frame++) {
            await new Promise(requestAnimationFrame);
            const settled = element.dataset.layoutSettled === "true";
            const height = element.scrollHeight;
            stableFrames = settled && height === previousHeight ? stableFrames + 1 : 0;
            previousHeight = height;
            if (stableFrames >= 3) return;
          }
          throw new Error("history layout did not remain settled for three animation frames");
        }"""
    )


async def test_upward_wheel_stays_detached_after_expanding_nested_toolsteps(visual: VisualHarness) -> None:
    async with visual.open(viewport=Viewport(width=1000, height=900), color_scheme="light") as view:
        page = view.page
        history = page.get_by_role("region", name="Conversation history", exact=True)
        await expect(history).to_have_attribute("data-layout-settled", "true")
        await _wait_for_settled_layout(history)
        await expect(history).to_have_attribute("data-scroll-mode", "following")
        assert await history.evaluate(
            "element => Math.abs(element.scrollHeight - element.clientHeight - element.scrollTop) <= 1"
        )

        header = history.locator('[data-history-anchor-key="tool-group-header"]')
        await expect(header).to_have_accessible_name("Expand near-bottom tool group")
        before_expand = await history.evaluate(
            "element => ({scrollTop: element.scrollTop, headerTop: element.querySelector('[data-history-anchor-key=tool-group-header]').getBoundingClientRect().top})"
        )
        await header.click()
        await expect(header).to_have_attribute("aria-expanded", "true")
        anchors = history.locator('[data-history-anchor-key^="tool-step:"]')
        await expect(anchors).to_have_count(6)
        await _wait_for_settled_layout(history)
        await expect(history).to_have_attribute("data-scroll-mode", "reading")
        after_expand = await history.evaluate(
            "element => ({scrollTop: element.scrollTop, headerTop: element.querySelector('[data-history-anchor-key=tool-group-header]').getBoundingClientRect().top})"
        )
        assert abs(after_expand["scrollTop"] - before_expand["scrollTop"]) <= 1, after_expand
        assert abs(after_expand["headerTop"] - before_expand["headerTop"]) <= 1, after_expand

        jump = history.get_by_role("button", name="Jump to latest", exact=True)
        await expect(jump).to_be_visible()
        await jump.click()
        await _wait_for_settled_layout(history)
        await expect(history).to_have_attribute("data-scroll-mode", "following")
        assert await history.evaluate(
            "element => Math.abs(element.scrollHeight - element.clientHeight - element.scrollTop) <= 1"
        )
        nested_edge = await history.evaluate(
            """element => {
              const top = element.getBoundingClientRect().top;
              const nested = [...element.querySelectorAll('[data-history-anchor-key^="tool-step:"]')]
                .find(node => {
                  const rect = node.getBoundingClientRect();
                  return rect.top <= top && rect.bottom > top;
                });
              if (!nested) return null;
              const row = nested.closest('[data-history-row-key]');
              return {
                key: nested.dataset.historyAnchorKey,
                inset: nested.getBoundingClientRect().top - row.getBoundingClientRect().top,
              };
            }"""
        )
        assert nested_edge is not None, nested_edge
        assert nested_edge["inset"] > 0, nested_edge

        await history.hover(position={"x": 40, "y": 80})
        start = await history.evaluate("element => element.scrollTop")
        await history.evaluate(
            """element => {
              const trace = {frame: 0, samples: [], scrollEnds: 0, ended: false, gestureDone: false, stableFrames: 0, stable: false};
              element.addEventListener("scroll", () => {
                trace.samples.push({frame: trace.frame, top: element.scrollTop});
                trace.ended = false;
                trace.stable = false;
                trace.stableFrames = 0;
              });
              element.addEventListener("scrollend", () => {
                trace.scrollEnds++;
                trace.ended = true;
                trace.scrollendTop = element.scrollTop;
              });
              const sample = () => {
                trace.frame++;
                if (trace.gestureDone && trace.ended && element.dataset.layoutSettled === "true") {
                  const previous = trace.lastTop ?? element.scrollTop;
                  trace.stableFrames = Math.abs(previous - element.scrollTop) <= 1 ? trace.stableFrames + 1 : 0;
                  trace.lastTop = element.scrollTop;
                  if (trace.stableFrames >= 3) trace.stable = true;
                } else {
                  trace.stableFrames = 0;
                  trace.lastTop = element.scrollTop;
                }
                requestAnimationFrame(sample);
              };
              window.__historyScrollTrace = trace;
              requestAnimationFrame(sample);
            }"""
        )

        bounds = await history.bounding_box()
        assert bounds is not None
        cdp = await page.context.new_cdp_session(page)
        try:
            await cdp.send(
                "Input.synthesizeScrollGesture",
                {
                    "x": bounds["x"] + bounds["width"] / 2,
                    "y": bounds["y"] + bounds["height"] / 2,
                    "gestureSourceType": "mouse",
                    "preventFling": True,
                    "speed": 180,
                    "yDistance": 300,
                },
            )
        finally:
            await cdp.detach()

        await page.evaluate("() => { window.__historyScrollTrace.gestureDone = true; }")
        await page.wait_for_function("() => window.__historyScrollTrace?.stable === true")
        await expect(history).to_have_attribute("data-layout-settled", "true")
        await expect(history).to_have_attribute("data-scroll-mode", "reading")
        trace = await page.evaluate("() => window.__historyScrollTrace")
        samples = trace["samples"]
        sampled_tops = [sample["top"] for sample in samples]
        assert trace["scrollEnds"] >= 1, trace
        assert len({sample["frame"] for sample in samples}) >= 4, trace
        assert min(sampled_tops) < start - 100, trace
        assert all(right <= left + 1 for left, right in pairwise(sampled_tops)), trace
        final_top = await history.evaluate("element => element.scrollTop")
        assert abs(final_top - trace["scrollendTop"]) <= 1, trace
        await view.capture("reading-after-upward-wheel", target=history)

        jump = history.get_by_role("button", name="Jump to latest", exact=True)
        await expect(jump).to_be_visible()
        await jump.click()
        await _wait_for_settled_layout(history)
        await expect(history).to_have_attribute("data-scroll-mode", "following")
        assert await history.evaluate(
            "element => Math.abs(element.scrollHeight - element.clientHeight - element.scrollTop) <= 1"
        )


if __name__ == "__main__":
    pytest_bazel.main()
