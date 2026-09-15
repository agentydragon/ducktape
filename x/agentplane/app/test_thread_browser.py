"""The built SPA consumes actual PostgreSQL-backed HTTP/SSE in a real Chromium page.

Only the upstream runner protocol source and Kubernetes/auth boundaries are controlled. Browser
fetch, EventSource, rendering, and page reload are not replaced by the visual harness's mocks.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_bazel
from playwright.async_api import Page, async_playwright, expect

from util.bazel.runfiles import get_required_path
from util.testing.frontend_visual import CONTAINER_BASE_BROWSER_ARGS, chromium_executable
from util.testing.undeclared_outputs import undeclared_outputs_dir
from x.agentplane.app.testing.replication_process import app_process
from x.agentplane.app.testing.replication_source import SANDBOX, SESSION, ReplicationSource
from x.agentplane.app.trajectory import TrajectoryStore
from x.agentplane.protocol import event_pb2

# gazelle:include_dep @pypi//protobuf


@pytest.fixture
async def page(request: pytest.FixtureRequest) -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, executable_path=chromium_executable(), args=CONTAINER_BASE_BROWSER_ARGS
        )
        try:
            async with await browser.new_context(viewport={"width": 1280, "height": 900}) as context:
                await context.tracing.start(screenshots=True, snapshots=True, sources=True)
                opened = await context.new_page()
                errors: list[str] = []
                opened.on("pageerror", lambda error: errors.append(str(error)))
                try:
                    yield opened
                    assert not errors, errors
                finally:
                    await context.tracing.stop(path=undeclared_outputs_dir() / f"{request.node.name}-trace.zip")
        finally:
            await browser.close()


@dataclass
class ThreadBrowser:
    page: Page
    source: ReplicationSource
    store: TrajectoryStore


@pytest.fixture
async def thread_browser(page: Page, db_url: str, store: TrajectoryStore) -> AsyncIterator[ThreadBrowser]:
    source = ReplicationSource()
    source.attached.active_turn_id = "test-browser-turn"
    source.append(event_pb2.Event(harness_started=event_pb2.HarnessStarted(pid=123)))
    source.append(
        event_pb2.Event(
            turn_started=event_pb2.TurnStarted(turn_id="test-browser-turn", model=source.attached.spec.model)
        )
    )
    source.append(
        event_pb2.Event(
            item_started=event_pb2.ItemStarted(item_id="test-browser-item", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
        )
    )
    source.append(
        event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="test-browser-item", text="Test retained prefix"))
    )
    directory = get_required_path("_main/x/agentplane/app/frontend/dist/index.html").parent
    async with source.serve() as target, app_process(db_url, target, frontend_directory=directory) as app:
        async with asyncio.timeout(30):
            opened = await source.opened.get()
            opened.replay.set()
            await page.goto(f"{app.url}/#/sandboxes/{SANDBOX}/sessions/{SESSION}")
            await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
        yield ThreadBrowser(page, source, store)


async def test_browser_replays_streams_and_reloads_one_exact_conversation(thread_browser: ThreadBrowser) -> None:
    page, source = thread_browser.page, thread_browser.source
    source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="test-browser-item", text=" and live suffix")))
    complete_text = "Test retained prefix and live suffix"
    await expect(page.get_by_text(complete_text, exact=True)).to_be_visible()
    source.append(
        event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id="test-browser-item", text=complete_text))
    )
    source.attached.active_turn_id = ""
    source.append(
        event_pb2.Event(
            turn_completed=event_pb2.TurnCompleted(turn_id="test-browser-turn", status=event_pb2.TURN_STATUS_COMPLETED)
        )
    )
    await expect(page.get_by_role("img", name="Streaming", exact=True)).to_have_count(0)
    await expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_disabled()

    # Reload replaces the JS document, including component memory and its EventSource. Durable
    # replay must rebuild the same text once, not append the prefix to the live card a second time.
    await page.reload()
    await expect(page.get_by_text(complete_text, exact=True)).to_have_count(1)
    await expect(page.get_by_role("img", name="Streaming", exact=True)).to_have_count(0)
    await expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_disabled()
    (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
    assert await thread_browser.store.events(thread.id, limit=100) == source.entries


if __name__ == "__main__":
    pytest_bazel.main()
