"""Capture an entire Grafana dashboard in kiosk mode."""

from playwright.async_api import expect

from util.testing.frontend_visual.capture import VisualPage


async def capture_dashboard(view: VisualPage) -> None:
    """Warm lazy panels, wait for Grafana queries, then capture the full page.

    Grafana Image Renderer scrolls the page to activate lazy content, waits for
    the Scenes query counter and network, then resizes to the full page height.
    """
    page = view.page
    await page.evaluate(
        """async () => {
            const scrolls = Math.floor(document.body.scrollHeight / window.innerHeight);
            for (let i = 0; i < scrolls; i++) {
                window.scrollBy(0, window.innerHeight);
                await new Promise(resolve => setTimeout(resolve, 50));
            }
            window.scrollTo(0, 0);
        }"""
    )
    await page.wait_for_function(
        "!window.__grafanaSceneContext || !(window.__grafanaRunningQueryCount > 0)", timeout=30000
    )
    await expect(page.get_by_text("Cancel", exact=True)).not_to_be_visible(timeout=30000)
    await view.check(context="dashboard after scrolling")
    for _ in range(5):
        height = await page.evaluate("document.body.scrollHeight")
        if height > 16000:
            raise AssertionError(f"dashboard height {height}px exceeds the supported screenshot size")
        viewport = page.viewport_size
        assert viewport is not None
        if height <= viewport["height"]:
            break
        await page.set_viewport_size({"width": viewport["width"], "height": height})
        await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        await page.wait_for_function(
            "!window.__grafanaSceneContext || !(window.__grafanaRunningQueryCount > 0)", timeout=30000
        )
        await expect(page.get_by_text("Cancel", exact=True)).not_to_be_visible(timeout=30000)
        await view.check(context="dashboard after resize")
    else:
        raise AssertionError("dashboard height did not stabilize")
    await view.capture("dashboard", full_page=True, animations="disabled")
