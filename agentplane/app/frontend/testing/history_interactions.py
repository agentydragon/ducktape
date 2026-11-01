"""Real Chromium gestures and position assertions shared by fixture and integration histories."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from playwright.async_api import Locator, Page

from agentplane.app.testing import thread_view_marks


async def frames(page: Page) -> None:
    """Wait for the paint after the next layout and its effects or observers."""
    await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


async def wheel(page: Page, delta: float) -> None:
    """One wheel gesture over the history, until it has ended and the app has adopted where it left
    the reader."""
    history = page.get_by_role("region", name="Thread history", exact=True)
    await history.hover()
    gesture = await history.evaluate_handle("""area => ({
        ended: new Promise(resolve => area.addEventListener("scrollend", resolve, { once: true }))
    })""")
    await page.mouse.wheel(0, round(delta))
    async with asyncio.timeout(30):
        await gesture.evaluate("gesture => gesture.ended")
    await gesture.dispose()


async def read_at(page: Page, target: Locator, fraction: float) -> None:
    """Wheels the history as a reader does until `target` sits `fraction` of the way down it, or the
    history ends, and the gesture has ended, so the app has adopted the place it left the reader at."""
    for _ in range(40):
        # One evaluation, without Locator.evaluate's wait: the virtualizer can unmount the row between
        # looking for it and measuring it.
        distance = await target.evaluate_all(
            """(elements, fraction) => {
                const [element] = elements;
                if (!element) return null;
                const area = element.closest('[aria-label="Thread history"]');
                const bounds = area.getBoundingClientRect();
                const wanted = element.getBoundingClientRect().top - bounds.top - fraction * bounds.height;
                return Math.min(wanted, area.scrollHeight - area.clientHeight - area.scrollTop);
            }""",
            fraction,
        )
        if distance is None:
            distance = -400  # Not mounted: it is further up than the rows that are.
        if abs(distance) <= 20:
            await frames(page)
            return
        await wheel(page, distance)
    raise AssertionError(f"scrolling never brought {target} to {fraction} of the way down the history")


@asynccontextmanager
async def holding_still(page: Page, line: Locator, *, rest_first: bool = True) -> AsyncIterator[None]:
    """Fails unless `line` stays where it is on screen, in every frame painted from here until the
    block's layout has settled: the reader's place is what they just clicked, and the history's
    scrolling may not carry it away. "Where it is" is where it is once the history says its layout
    has come to rest (`data-layout-settled`): it keeps measuring rows, and moving the ones it has
    laid out, for a while after it loads, and that shift is not the click's. `rest_first=False` for
    a layout that keeps changing by design, such as output streaming in below the line."""
    try:
        async with asyncio.timeout(30):
            watch = await line.evaluate_handle(
                """(element, restFirst) => new Promise(resolve => {
                    const area = document.querySelector('[aria-label="Thread history"]');
                    const state = { drift: 0, detached: false, stopped: false };
                    const sample = start => {
                        if (state.stopped) return;
                        if (element.isConnected) {
                            state.drift = Math.max(state.drift, Math.abs(element.getBoundingClientRect().top - start));
                        } else state.detached = true;
                        requestAnimationFrame(() => sample(start));
                    };
                    const begin = () => requestAnimationFrame(() => {
                        if (restFirst && area.dataset.layoutSettled !== "true") return begin();
                        sample(element.getBoundingClientRect().top);
                        resolve(state);
                    });
                    begin();
                })""",
                rest_first,
            )
    except TimeoutError:
        raise AssertionError(
            f"{line} never came to rest; the history's last events:\n{await thread_view_marks.recent(page, 40)}"
        ) from None
    try:
        yield
        await frames(page)
        outcome = await watch.evaluate(
            "state => { state.stopped = true; return { drift: state.drift, detached: state.detached }; }"
        )
    finally:
        await watch.dispose()
    assert not outcome["detached"], (
        f"{line} left the page; the history's last events:\n{await thread_view_marks.recent(page, 60)}"
    )
    assert outcome["drift"] <= 2, (
        f"{line} moved {outcome['drift']}px from where it was clicked; the history's last events:\n"
        f"{await thread_view_marks.recent(page, 60)}"
    )


def tool_call_in(run: Locator, name: str) -> tuple[Locator, Locator]:
    """A call in `run` and the card around it, whose top edge holds still as the call opens."""
    selector = ".transcript-step-details"
    call = run.locator(selector, has_text=f"Run tool {name}")
    card = call.locator(
        "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' transcript-inspection-owner ')][1]"
    )
    return call, card
