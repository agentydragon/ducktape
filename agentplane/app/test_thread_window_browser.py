"""Real-browser acceptance for a thread's window: one shape, paged back into, resumed after a gap."""

import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest_bazel
from playwright.async_api import expect

from agentplane.app.test_thread_browser import ThreadBrowser, db_url
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


def _handles(urls: list[str]) -> set[str]:
    """The entity shape handles a set of requests named."""
    return {
        handle for url in urls if "/sync/entities?" in url for handle in parse_qs(urlsplit(url).query).get("handle", [])
    }


async def test_a_growing_thread_stays_one_shape_and_scrolling_back_keeps_the_reader_s_place(
    thread_browser: ThreadBrowser,
) -> None:
    page = thread_browser.page
    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible(timeout=30_000)
    latest = _append_items(thread_browser, "window-item", range(70))
    await expect(page.locator(f'[data-projection-cursor="{latest.cursor}"]')).to_have_count(1, timeout=30_000)

    # Opened now, the thread is longer than its tail: older rows are a page away.
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    await page.reload()
    await expect(page.locator(f'[data-projection-cursor="{latest.cursor}"]')).to_have_count(1, timeout=30_000)
    await expect(page.get_by_text("Window message 069", exact=False)).to_be_visible()
    composer = page.get_by_placeholder("Enter sends, Ctrl+Enter for a new line")
    await composer.fill("Draft retained while the thread grows")
    # Virtualization keeps only the measured viewport and overscan mounted.
    assert await page.locator("[data-thread-anchor]").count() < 20

    history = page.get_by_role("region", name="Thread history", exact=True)
    async with page.expect_request(
        lambda request: request.method == "POST" and "entity_index < $1" in (request.post_data or "")
    ):
        await page.get_by_role("button", name="Load 30 earlier", exact=True).click()
    await history.hover()
    # Reaching the top loads the page before it, and the reader's row is held in place while it
    # lands; sample the position once two consecutive frames agree.
    gesture = await history.evaluate_handle(
        "area => ({ ended: new Promise(resolve => area.addEventListener('scrollend', () => resolve(), { once: true })) })"
    )
    await page.mouse.wheel(0, -10_000)
    async with asyncio.timeout(30):
        await gesture.evaluate("gesture => gesture.ended")
        anchor = await history.evaluate(
            """area => new Promise(resolve => {
                const sample = () => {
                    const top = area.getBoundingClientRect().top;
                    const row = [...area.querySelectorAll('[data-thread-anchor]')]
                        .find(candidate => candidate.getBoundingClientRect().bottom > top);
                    return { cursor: row.dataset.threadAnchor, top: row.getBoundingClientRect().top };
                };
                const settle = previous => requestAnimationFrame(() => {
                    const current = sample();
                    if (current.cursor === previous.cursor && current.top === previous.top) resolve(current);
                    else settle(current);
                });
                requestAnimationFrame(() => settle(sample()));
            })"""
        )
    await gesture.dispose()

    # The tail grows by more than a page while the reader stays back in the thread: its rows
    # arrive on the same shape, and the row the reader is at does not move.
    latest = _append_items(thread_browser, "window-item", range(70, 105))
    await expect(page.locator(f'[data-projection-cursor="{latest.cursor}"]')).to_have_count(1, timeout=30_000)
    restored = page.locator(f'[data-thread-anchor="{anchor["cursor"]}"]')
    await expect(restored).to_have_count(1)
    assert abs(await restored.evaluate("row => row.getBoundingClientRect().top") - anchor["top"]) <= 2
    await expect(composer).to_have_value("Draft retained while the thread grows")

    # One scope read and one shape since the reload: the window moved by loading more into it.
    assert len([url for url in requests if "/sync/scope" in url]) == 1
    assert len(_handles(requests)) == 1
    await page.screenshot(path=undeclared_outputs_dir() / "thread-window-retained-reader.png")


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
