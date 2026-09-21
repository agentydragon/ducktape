"""Real-browser acceptance for bounded projected conversation window rotation."""

import asyncio

import pytest_bazel
from playwright.async_api import Request, expect

from agentplane.app.test_thread_browser import ThreadBrowser
from agentplane.protocol import event_pb2

pytest_plugins = ("agentplane.app.test_thread_browser",)


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

    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible(timeout=30_000)
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
    assert stale_status == 409

    # The live collection rotates before it retains more than two 30-row pages, while DOM
    # virtualization keeps only the measured viewport and overscan mounted.
    assert len(interest_urls) < 10, interest_urls
    assert await page.locator("[data-conversation-anchor]").count() < 20

    interest_seen.clear()
    await page.get_by_role("button", name="Load 30 earlier", exact=True).click()
    async with asyncio.timeout(10):
        await interest_seen.wait()
    history = page.get_by_role("region", name="Thread history", exact=True)
    interest_seen.clear()
    anchor = await history.evaluate(
        """area => {
            area.scrollTop = 0;
            const row = [...area.querySelectorAll('[data-conversation-anchor]')]
                .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)[0];
            return { cursor: row.dataset.conversationAnchor, top: row.getBoundingClientRect().top };
        }"""
    )
    async with asyncio.timeout(10):
        await interest_seen.wait()
    restored = page.locator(f'[data-conversation-anchor="{anchor["cursor"]}"]')
    await expect(restored).to_have_count(1)
    assert abs(await restored.evaluate("row => row.getBoundingClientRect().top") - anchor["top"]) <= 2
    await expect(composer).to_have_value("Draft retained across shape rotation")


if __name__ == "__main__":
    pytest_bazel.main()
