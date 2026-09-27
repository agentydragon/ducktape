"""Real-browser acceptance for a thread's window: one shape, paged back into, resumed after a gap."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import pytest_bazel
from playwright.async_api import Page, Request, Route, TimeoutError as PlaywrightTimeoutError, expect

from agentplane.app.test_thread_browser import ThreadBrowser, capture_reading_anchor, db_url, expect_reading_anchor
from agentplane.protocol import event_log_pb2, event_pb2
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf

pytest_plugins = ("agentplane.app.test_thread_browser",)
__all__ = ["db_url"]


def _append_items(thread_browser: ThreadBrowser, prefix: str, numbers: range) -> event_log_pb2.EventEntry:
    latest = None
    for number in numbers:
        item_id = f"{prefix}-{number:03d}"
        thread_browser.source.append(
            event_pb2.Event(
                item_started=event_pb2.ItemStarted(item_id=item_id, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
            )
        )
        latest = thread_browser.source.append(
            event_pb2.Event(
                item_completed=event_pb2.ItemCompleted(
                    item_id=item_id,
                    text=f"Window message {number:03d}: " + "measured variable-height text " * (number % 4 + 1),
                )
            )
        )
    assert latest is not None
    return latest


def _append_uneven_items(thread_browser: ThreadBrowser, prefix: str, numbers: range) -> event_log_pb2.EventEntry:
    """Alternates a one-line message with one many times taller, so a page of them diverges wildly
    from VirtualizedHistory's fixed 180px `estimateSize` -- unlike _append_items's mild 1x-4x
    variance, this maximizes the gap scrollToIndex's estimate has to correct for an unmounted row."""
    latest = None
    for number in numbers:
        item_id = f"{prefix}-{number:03d}"
        text = (
            f"Short {number:03d}"
            if number % 3
            else f"Tall message {number:03d}\n\n" + "A much longer measured paragraph of reading text. " * 20
        )
        thread_browser.source.append(
            event_pb2.Event(
                item_started=event_pb2.ItemStarted(item_id=item_id, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
            )
        )
        latest = thread_browser.source.append(
            event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id=item_id, text=text))
        )
    assert latest is not None
    return latest


def _handles(urls: list[str]) -> set[str]:
    """The entity shape handles a set of requests named."""
    return {
        handle for url in urls if "/sync/entities?" in url for handle in parse_qs(urlsplit(url).query).get("handle", [])
    }


def _older_page(request: Request) -> bool:
    """Whether a request is for the page before the oldest row the reader holds."""
    return request.method == "POST" and "entity_index < $1" in (request.post_data or "")


def _recording_requests(page: Page) -> list[Request]:
    """Every request the page makes from now on."""
    requests: list[Request] = []

    # Playwright cannot register a builtin method such as `list.append` as a handler.
    def record(request: Request) -> None:
        requests.append(request)

    page.on("request", record)
    return requests


@dataclass
class _HeldPages:
    """Set once the browser asks for a page before its oldest row; that request waits for `release`."""

    asked: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


@asynccontextmanager
async def _holding_older_pages(page: Page) -> AsyncIterator[_HeldPages]:
    held = _HeldPages()

    async def hold(route: Route) -> None:
        if not _older_page(route.request):
            await route.fallback()
            return
        held.asked.set()
        await held.release.wait()
        await route.continue_()

    await page.route("**/sync/entities?*", hold)
    try:
        yield held
    finally:
        held.release.set()
        await page.unroute("**/sync/entities?*", hold)


async def _frames(page: Page) -> None:
    """Waits for the paint after the next layout, and any effect or observer it runs."""
    await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


async def test_a_growing_thread_stays_one_shape_and_scrolling_back_keeps_the_reader_s_place(
    thread_browser: ThreadBrowser,
) -> None:
    page = thread_browser.page
    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible(timeout=30_000)
    latest = _append_items(thread_browser, "window-item", range(70))
    await expect(page.locator(f'[data-projection-cursor="{latest.cursor}"]')).to_have_count(1, timeout=30_000)

    # Opened now, the thread is longer than its tail: older rows are a page away.
    requests = _recording_requests(page)
    await page.reload()
    await expect(page.locator(f'[data-projection-cursor="{latest.cursor}"]')).to_have_count(1, timeout=30_000)
    await expect(page.get_by_text("Window message 069", exact=False)).to_be_visible()
    composer = page.get_by_placeholder("Enter sends, Ctrl+Enter for a new line")
    await composer.fill("Draft retained while the thread grows")
    # Virtualization keeps only the measured viewport and overscan mounted: a loose bound well
    # below the 70 rows appended, tolerant of row-height changes rather than pinned to one.
    assert await page.locator("[data-thread-anchor]").count() < 40
    # The tail fills the view: nothing older loads before the reader scrolls up to it.
    await _frames(page)
    assert not [request for request in requests if _older_page(request)]

    history = page.get_by_role("region", name="Thread history", exact=True)
    loading = history.get_by_role("status").filter(has_text="Loading earlier…")
    await history.hover()
    async with _holding_older_pages(page) as held:
        # Reaching the top asks for the page before it. Sample the reader's row while that page is
        # still on its way, once the gesture has ended and two consecutive frames agree.
        gesture = await history.evaluate_handle(
            "area => ({ ended: new Promise(resolve => area.addEventListener('scrollend', () => resolve(), { once: true })) })"
        )
        await page.mouse.wheel(0, -10_000)
        async with asyncio.timeout(30):
            await held.asked.wait()
            await gesture.evaluate("gesture => gesture.ended")
            anchor = await capture_reading_anchor(history)
        await gesture.dispose()
        await expect(loading).to_be_visible()
        await page.screenshot(path=undeclared_outputs_dir() / "thread-window-loading-earlier.png")
        held.release.set()
        # The page lands above the reader, whose row stays where it was.
        await expect(loading).to_have_count(0)
        await expect_reading_anchor(page, anchor)

    # The tail grows by more than a page while the reader stays back in the thread: its rows
    # arrive on the same shape, and the row the reader is at does not move.
    latest = _append_items(thread_browser, "window-item", range(70, 105))
    await expect(page.locator(f'[data-projection-cursor="{latest.cursor}"]')).to_have_count(1, timeout=30_000)
    restored = page.locator(f'[data-thread-anchor="{anchor["cursor"]}"]')
    await expect(restored).to_have_count(1)
    assert abs(await restored.evaluate("row => row.getBoundingClientRect().top") - anchor["top"]) <= 2
    await expect(composer).to_have_value("Draft retained while the thread grows")

    # One scope read and one shape since the reload: the window moved by loading more into it, one
    # page for the one time the reader reached its top.
    urls = [request.url for request in requests]
    assert len([url for url in urls if "/sync/scope" in url]) == 1
    assert len(_handles(urls)) == 1
    assert len([request for request in requests if _older_page(request)]) == 1
    await page.screenshot(path=undeclared_outputs_dir() / "thread-window-retained-reader.png")


async def test_an_older_page_landing_mid_gesture_does_not_move_the_reader(thread_browser: ThreadBrowser) -> None:
    """test_a_growing_thread_stays_one_shape... releases the held older-page response only after
    the scroll-up gesture has already settled (scrollend fired). A real fast scroll can instead
    have that response land while the browser is still between the scroll and its later
    scrollend -- the race a live report described as the view jumping while scrolling up to see
    the loading-earlier banner."""
    page = thread_browser.page
    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible(timeout=30_000)
    latest = _append_items(thread_browser, "window-item", range(70))
    await expect(page.locator(f'[data-projection-cursor="{latest.cursor}"]')).to_have_count(1, timeout=30_000)
    await page.reload()
    await expect(page.locator(f'[data-projection-cursor="{latest.cursor}"]')).to_have_count(1, timeout=30_000)

    history = page.get_by_role("region", name="Thread history", exact=True)
    loading = history.get_by_role("status").filter(has_text="Loading earlier…")
    await history.hover()
    async with _holding_older_pages(page) as held:
        gesture = await history.evaluate_handle(
            "area => ({ ended: new Promise(resolve => area.addEventListener('scrollend', () => resolve(), { once: true })) })"
        )
        await page.mouse.wheel(0, -10_000)
        async with asyncio.timeout(30):
            await held.asked.wait()
        # Unlike test_a_growing_thread_stays_one_shape..., release before the gesture settles: the
        # response lands while the browser is still between the scroll and its later scrollend.
        anchor = await capture_reading_anchor(history)
        held.release.set()
        await expect(loading).to_have_count(0)
        async with asyncio.timeout(30):
            await gesture.evaluate("gesture => gesture.ended")
        await gesture.dispose()
        await _frames(page)
        await expect_reading_anchor(page, anchor)
        await page.screenshot(path=undeclared_outputs_dir() / "thread-window-mid-gesture-prepend.png")


async def test_an_older_page_landing_mid_gesture_does_not_restore_an_earlier_settled_anchor(
    thread_browser: ThreadBrowser,
) -> None:
    """The reader's anchor is only recaptured at a gesture's scrollend (the app's onScrollEnd calls
    captureReadingAnchor there, not on every scroll event). test_an_older_page_landing_mid_gesture...
    covers a reader's *first* scroll of the session, before any anchor exists, and passes because
    the app has nothing stale to restore to. A reader who already settled one scroll earlier, then
    scrolls again and reaches the top before this second gesture's own scrollend, has an older page
    land while the last captured anchor is still the position of that first, already-settled
    gesture. Restoring to it would pull the reader back down to where they read before -- not
    where the second gesture left them. Not yet observed to reproduce here: a single synthetic
    wheel jump settles (scrollend) faster than the held response's round trip through this test's
    route interception, so the anchor is already fresh again by the time rows change. Kept as
    coverage for the invariant regardless."""
    page = thread_browser.page
    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible(timeout=30_000)
    latest = _append_items(thread_browser, "window-item", range(90))
    await expect(page.locator(f'[data-projection-cursor="{latest.cursor}"]')).to_have_count(1, timeout=30_000)
    await page.reload()
    await expect(page.locator(f'[data-projection-cursor="{latest.cursor}"]')).to_have_count(1, timeout=30_000)

    history = page.get_by_role("region", name="Thread history", exact=True)
    await history.hover()

    # A first gesture settles well short of the top: this captures a real reading anchor there,
    # without reaching LOAD_OLDER_WITHIN (the initial tail is only PAGE=30 rows -- too big a wheel
    # here reaches scrollTop=0 and fires this gesture's own, unheld, loadOlder()).
    first_gesture = await history.evaluate_handle(
        "area => ({ ended: new Promise(resolve => area.addEventListener('scrollend', () => resolve(), { once: true })) })"
    )
    await page.mouse.wheel(0, -200)
    async with asyncio.timeout(30):
        await first_gesture.evaluate("gesture => gesture.ended")
    await first_gesture.dispose()
    assert await history.evaluate("area => area.scrollTop") > 80
    first_anchor = await capture_reading_anchor(history)

    async with _holding_older_pages(page) as held:
        # A second, separate gesture continues toward the top and reaches LOAD_OLDER_WITHIN, which
        # asks for the older page from inside onScroll itself -- well before this gesture's own
        # scrollend, so the app's last captured anchor is still `first_anchor`.
        await page.mouse.wheel(0, -10_000)
        async with asyncio.timeout(30):
            await held.asked.wait()
        second_anchor = await capture_reading_anchor(history)
        assert second_anchor["cursor"] != first_anchor["cursor"]
        held.release.set()
        await _frames(page)
        await _frames(page)
        # The reader stays where the second gesture left them, not pulled back to the first.
        await expect_reading_anchor(page, second_anchor)
        await page.screenshot(path=undeclared_outputs_dir() / "thread-window-stale-anchor.png")


async def test_scrolling_up_continuously_through_a_landing_older_page_does_not_jump_to_the_bottom(
    thread_browser: ThreadBrowser,
) -> None:
    """A trackpad/touch scroll dispatches many small wheel events over time, not one big one --
    unlike test_an_older_page_landing_mid_gesture_does_not_move_the_reader, several of them can
    land while the older page's rows are still being measured one at a time (each starts at the
    180px estimate and is re-measured as it mounts), not just before or after that process."""
    page = thread_browser.page
    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible(timeout=30_000)
    latest = _append_items(thread_browser, "window-item", range(70))
    await expect(page.locator(f'[data-projection-cursor="{latest.cursor}"]')).to_have_count(1, timeout=30_000)
    await page.reload()
    await expect(page.locator(f'[data-projection-cursor="{latest.cursor}"]')).to_have_count(1, timeout=30_000)

    history = page.get_by_role("region", name="Thread history", exact=True)
    await history.hover()
    async with _holding_older_pages(page) as held:
        await page.mouse.wheel(0, -3_000)
        async with asyncio.timeout(30):
            await held.asked.wait()
        held.release.set()
        for _ in range(20):
            await page.mouse.wheel(0, -150)
    await _frames(page)
    await _frames(page)
    await _frames(page)
    geometry = await history.evaluate(
        "area => ({top: area.scrollTop, bottom: area.scrollHeight - area.clientHeight - area.scrollTop})"
    )
    # A reader scrolling up through the landing page must not end up at the tail.
    assert geometry["bottom"] > 24, geometry


async def test_repeated_pagination_through_wildly_uneven_row_heights_keeps_the_reader_s_place(
    thread_browser: ThreadBrowser,
) -> None:
    """A live report described the jump as reliable across repeated reloads of one real thread --
    not an occasional race, but something that keeps happening on that thread's actual content.
    estimateSize is a flat 180px; a page of rows nowhere near that (a one-liner next to a message
    twenty paragraphs long) makes virtualizer.scrollToIndex's estimate-based jump, and the two
    frames of correctFromDom afterwards, wrong by a large and inconsistent margin each cycle --
    unlike _append_items's mild variance, which every existing pagination test here uses. Runs
    several scroll-to-top cycles deep into that unevenness, each one fully settled before the
    next, to isolate accumulated estimate error from any gesture-timing race."""
    page = thread_browser.page
    console: list[str] = []
    page.on("console", lambda msg: console.append(msg.text))
    # Surfaces VirtualizedHistory's own scroll-anchor trace (projected_session.tsx's scrollDebug)
    # in the dump below on failure, since this test's whole point is diagnosing exactly that path.
    await page.add_init_script("localStorage.setItem('agentplane:debugScroll', '1')")
    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible(timeout=30_000)
    latest = _append_uneven_items(thread_browser, "uneven-item", range(210))
    await expect(page.locator(f'[data-projection-cursor="{latest.cursor}"]')).to_have_count(1, timeout=30_000)
    await page.reload()
    await expect(page.locator(f'[data-projection-cursor="{latest.cursor}"]')).to_have_count(1, timeout=30_000)
    console.clear()

    history = page.get_by_role("region", name="Thread history", exact=True)
    loading = history.get_by_role("status").filter(has_text="Loading earlier…")
    await history.hover()

    for cycle in range(6):
        console.clear()
        gesture = await history.evaluate_handle(
            "area => ({ ended: new Promise(resolve => area.addEventListener('scrollend', () => resolve(), { once: true })) })"
        )
        await page.mouse.wheel(0, -20_000)
        async with asyncio.timeout(30):
            await gesture.evaluate("gesture => gesture.ended")
        await gesture.dispose()
        anchor = await capture_reading_anchor(history)
        await expect(loading).to_have_count(0, timeout=30_000)
        await _frames(page)
        await _frames(page)
        try:
            await expect_reading_anchor(page, anchor)
        except PlaywrightTimeoutError:
            raise AssertionError(f"cycle {cycle} anchor={anchor}\n" + "\n".join(console)) from None
        await page.screenshot(path=undeclared_outputs_dir() / f"thread-window-uneven-cycle-{cycle}.png")


async def test_a_tail_too_short_to_scroll_loads_the_rows_before_it_unasked(thread_browser: ThreadBrowser) -> None:
    page = thread_browser.page
    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible(timeout=30_000)
    latest = _append_items(thread_browser, "short-item", range(32))
    await expect(page.locator(f'[data-projection-cursor="{latest.cursor}"]')).to_have_count(1, timeout=30_000)

    # Opened again in a view taller than its tail, the thread has no scrollbar, so no scroll can
    # reach the top to ask for the few rows before the tail.
    await page.set_viewport_size({"width": 1280, "height": 8000})
    requests = _recording_requests(page)
    history = page.get_by_role("region", name="Thread history", exact=True)
    async with _holding_older_pages(page) as held:
        await page.reload()
        async with asyncio.timeout(30):
            await held.asked.wait()
        await expect(history.get_by_role("status").filter(has_text="Loading earlier…")).to_be_visible()
        assert await history.evaluate("area => area.scrollHeight <= area.clientHeight")
        await expect(page.get_by_text("Test retained prefix", exact=True)).to_have_count(0)
        await page.screenshot(
            path=undeclared_outputs_dir() / "thread-window-short-loading-earlier.png",
            clip={"x": 0, "y": 0, "width": 1280, "height": 1000},
        )
        held.release.set()
        await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    await expect(history.get_by_role("status")).to_have_count(0)
    await expect(page.get_by_text("Window message 031", exact=False)).to_be_visible()
    # That page held the rest of the thread, so nothing more is asked for.
    await _frames(page)
    assert len([request for request in requests if _older_page(request)]) == 1


async def test_a_long_offline_gap_resumes_the_same_shape_without_losing_the_draft(
    thread_browser: ThreadBrowser,
) -> None:
    page, store = thread_browser.page, thread_browser.store
    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    (thread,) = await store.list_threads()
    composer = page.get_by_placeholder("Enter sends, Ctrl+Enter for a new line")
    await composer.fill("Draft retained across a long offline gap")
    document = await page.evaluate_handle("document")
    before = _handles(await page.evaluate("() => performance.getEntriesByType('resource').map(entry => entry.name)"))
    assert len(before) == 1
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))

    await page.context.set_offline(True)
    try:
        # Offline fails only new requests; the live SSE response ends with its connection.
        async with page.expect_event("requestfailed", predicate=lambda request: "/sync/entities?" in request.url):
            await thread_browser.ingress.drop_connections()
        latest = _append_items(thread_browser, "offline-item", range(70))
        async with asyncio.timeout(15):
            while True:
                scope = await thread_browser.content.current_scope(thread.id)
                if scope is not None and scope.through_cursor >= latest.cursor:
                    break
                await asyncio.sleep(0.01)
        await expect(composer).to_have_value("Draft retained across a long offline gap")
        async with page.expect_response(lambda response: "/sync/entities?" in response.url and response.ok):
            await page.context.set_offline(False)
        await expect(page.locator(f'[data-projection-cursor="{latest.cursor}"]')).to_have_count(1, timeout=30_000)
        await expect(page.get_by_text("Window message 069", exact=False)).to_be_visible()
        await expect(composer).to_have_value("Draft retained across a long offline gap")
        assert await document.evaluate("original => original === document")
        # The gap is read from the shape's log, from the offset the reader had reached.
        assert _handles(requests) == before
        assert not [url for url in requests if "/sync/scope" in url]
        await page.screenshot(path=undeclared_outputs_dir() / "thread-long-offline-recovery.png")
    finally:
        await page.context.set_offline(False)
        await document.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
