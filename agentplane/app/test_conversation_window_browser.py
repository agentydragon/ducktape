"""Real-browser acceptance for bounded projected conversation window rotation."""

import asyncio
import json

import pytest_bazel
from playwright.async_api import Request, expect

from agentplane.app.test_thread_browser import ThreadBrowser, db_url
from agentplane.protocol import event_pb2
from util.testing.undeclared_outputs import undeclared_outputs_dir

pytest_plugins = ("agentplane.app.test_thread_browser",)
__all__ = ["db_url"]


async def test_large_live_tail_rotates_and_preserves_reader_state(thread_browser: ThreadBrowser) -> None:
    page, source = thread_browser.page, thread_browser.source
    entity_urls: list[str] = []
    interest_urls: list[str] = []
    interest_seen = asyncio.Event()

    def observe_request(request: Request) -> None:
        url = request.url
        if "/sync/entities?" in url:
            entity_urls.append(url)
        elif "/sync/interest" in url:
            interest_urls.append(url)
            interest_seen.set()

    page.on("request", observe_request)
    await page.evaluate("() => { window.__agentplaneConversationCollectionTrace = []; }")

    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible(timeout=30_000)
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("HeapProfiler.collectGarbage")
    heap_before = await cdp.send("Runtime.getHeapUsage")
    initial_entity_url = await page.evaluate(
        """() => performance.getEntriesByType('resource')
            .map(entry => entry.name).find(name => name.includes('/sync/entities?'))"""
    )
    assert initial_entity_url
    composer = page.get_by_placeholder("Enter sends, Ctrl+Enter for a new line")
    await composer.fill("Draft retained across shape rotation")

    for number in range(70):
        item_id = f"window-item-{number:03d}"
        source.append(
            event_pb2.Event(
                item_started=event_pb2.ItemStarted(item_id=item_id, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
            )
        )
        latest = source.append(
            event_pb2.Event(
                item_completed=event_pb2.ItemCompleted(
                    item_id=item_id,
                    text=f"Window message {number:03d}: " + "measured variable-height text " * (number % 4 + 1),
                )
            )
        )

    session = page.locator(f'[data-projection-cursor="{latest.cursor}"]')
    await expect(session).to_have_count(1, timeout=30_000)
    await expect(composer).to_have_value("Draft retained across shape rotation")
    await expect(page.get_by_text("Conversation synchronization stopped.", exact=True)).to_have_count(0)
    await expect(page.get_by_text("Window message 069", exact=False)).to_be_visible()

    # The original fixed shape is now more than two pages behind. Reusing it must yield an
    # explicit bounded refresh signal instead of replaying the growing suffix.
    stale_status = await page.evaluate(
        """async (raw) => {
            const url = new URL(raw);
            url.searchParams.set('live', 'false');
            url.searchParams.set('offset', '-1');
            return (await fetch(url)).status;
        }""",
        initial_entity_url,
    )
    assert stale_status == 410

    # The live collection rotates before it retains more than two 30-row pages, while DOM
    # virtualization keeps only the measured viewport and overscan mounted.
    assert len(interest_urls) < 10, interest_urls
    assert await page.locator("[data-conversation-anchor]").count() < 20

    interest_seen.clear()
    await page.get_by_role("button", name="Load 30 earlier", exact=True).click()
    async with asyncio.timeout(10):
        await interest_seen.wait()
    history = page.get_by_role("region", name="Thread history", exact=True)
    await page.wait_for_function(
        """() => window.__agentplaneConversationCollectionTrace?.some(event =>
            event.kind === 'query' && event.role === 'active' && event.ready && !event.id.endsWith(':tail')
        )"""
    )
    interest_seen.clear()
    await history.hover()
    await page.mouse.wheel(0, -10_000)
    await page.wait_for_function(
        """() => {
            const area = document.querySelector('[aria-label="Thread history"]');
            return area.scrollTop < 80;
        }"""
    )
    await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    anchor = await history.evaluate(
        """area => {
            const top = area.getBoundingClientRect().top;
            const row = [...area.querySelectorAll('[data-conversation-anchor]')]
                .find(candidate => candidate.getBoundingClientRect().bottom > top);
            return { cursor: row.dataset.conversationAnchor, top: row.getBoundingClientRect().top };
        }"""
    )
    # Advance the live tail by another page while the reader remains in the older
    # window. The current tail shape must rotate without replacing that reading
    # window or moving its measured anchor.
    for number in range(70, 105):
        item_id = f"window-item-{number:03d}"
        source.append(
            event_pb2.Event(
                item_started=event_pb2.ItemStarted(item_id=item_id, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
            )
        )
        latest = source.append(
            event_pb2.Event(
                item_completed=event_pb2.ItemCompleted(
                    item_id=item_id, text=f"Window message {number:03d}: " + "later live text " * (number % 3 + 1)
                )
            )
        )
    async with asyncio.timeout(10):
        await interest_seen.wait()
    await expect(page.locator(f'[data-projection-cursor="{latest.cursor}"]')).to_have_count(1, timeout=30_000)
    restored = page.locator(f'[data-conversation-anchor="{anchor["cursor"]}"]')
    await expect(restored).to_have_count(1)
    assert abs(await restored.evaluate("row => row.getBoundingClientRect().top") - anchor["top"]) <= 2
    await expect(composer).to_have_value("Draft retained across shape rotation")
    await page.wait_for_function(
        """() => window.__agentplaneConversationCollectionTrace?.some(event =>
            event.kind === 'collected' && event.status === 'cleaned-up' && event.size === 0 &&
            event.subscriberCount === 0
        )"""
    )
    await cdp.send("HeapProfiler.collectGarbage")
    heap_after = await cdp.send("Runtime.getHeapUsage")
    trace = await page.evaluate("() => window.__agentplaneConversationCollectionTrace")
    (undeclared_outputs_dir() / "conversation-window-resources.json").write_text(
        json.dumps({"collections": trace, "heap_before": heap_before, "heap_after": heap_after}, indent=2)
    )
    ready = [event for event in trace if event["kind"] == "query" and event["ready"]]
    assert ready
    assert all(event["subscriberCount"] >= 1 for event in ready)
    active = [event for event in ready if event["role"] == "active"]
    assert active[-1]["size"] <= 61


async def test_long_offline_gap_refreshes_expired_interest_without_losing_draft(thread_browser: ThreadBrowser) -> None:
    page, source, store = thread_browser.page, thread_browser.source, thread_browser.store
    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    (thread,) = await store.list_threads()
    composer = page.get_by_placeholder("Enter sends, Ctrl+Enter for a new line")
    await composer.fill("Draft retained across a long offline gap")
    document = await page.evaluate_handle("document")

    await page.context.set_offline(True)
    try:
        for number in range(70):
            item_id = f"offline-item-{number}"
            source.append(
                event_pb2.Event(
                    item_started=event_pb2.ItemStarted(item_id=item_id, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
                )
            )
            latest = source.append(
                event_pb2.Event(
                    item_completed=event_pb2.ItemCompleted(item_id=item_id, text=f"Offline message {number}")
                )
            )
        async with asyncio.timeout(15):
            while True:
                scope = await store.current_conversation_scope(thread.id)
                if scope is not None and scope.through_cursor >= latest.cursor:
                    break
                await asyncio.sleep(0.01)
        await expect(composer).to_have_value("Draft retained across a long offline gap")
        async with page.expect_response(lambda response: "/sync/entities?" in response.url and response.status == 410):
            await page.context.set_offline(False)
        await expect(page.locator(f'[data-projection-cursor="{latest.cursor}"]')).to_have_count(1, timeout=30_000)
        await expect(page.get_by_text("Offline message 69", exact=True)).to_be_visible()
        await expect(page.get_by_text("Test retained prefix", exact=True)).to_have_count(0)
        await expect(composer).to_have_value("Draft retained across a long offline gap")
        assert await document.evaluate("original => original === document")
        await page.screenshot(path=undeclared_outputs_dir() / "conversation-long-offline-recovery.png")
    finally:
        await page.context.set_offline(False)
        await document.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
