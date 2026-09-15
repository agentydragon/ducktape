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
from x.agentplane.app.testing.replication_source import SANDBOX, SESSION, Opened, ReplicationSource
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
    opened: Opened


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
            await page.goto(f"{app.url}/#/sandboxes/{SANDBOX}/sessions/{SESSION}")
        yield ThreadBrowser(page, source, store, opened)


async def test_browser_replays_streams_and_reloads_one_exact_conversation(thread_browser: ThreadBrowser) -> None:
    page, source = thread_browser.page, thread_browser.source
    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
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


async def test_ahead_snapshot_is_not_a_conversation_or_effective_model(thread_browser: ThreadBrowser) -> None:
    page = thread_browser.page
    await expect(page.get_by_role("status")).to_have_text("Catching up: 0 / 4 events")
    await expect(page.get_by_role("combobox", name="Model", exact=True)).to_have_value("")
    await expect(page.get_by_role("combobox", name="Model", exact=True)).to_be_disabled()
    await expect(page.get_by_placeholder("Enter sends, Ctrl+Enter for a new line")).to_be_disabled()
    await expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_disabled()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_have_count(0)
    (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
    assert await thread_browser.store.last_cursor(thread.id) == 0

    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    await expect(page.get_by_role("status")).to_have_count(0)
    await expect(page.get_by_role("combobox", name="Model", exact=True)).to_have_value("test-model-before")
    await expect(page.get_by_role("combobox", name="Model", exact=True)).to_be_enabled()
    await expect(page.get_by_placeholder("Enter sends, Ctrl+Enter for a new line")).to_be_enabled()
    await expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_enabled()
    assert await thread_browser.store.events(thread.id, limit=100) == thread_browser.source.entries


@pytest.mark.parametrize(
    ("fault", "reason"),
    [("gap", "expected runner cursor 5, received 6"), ("source-change", "runner source changed at cursor 5")],
)
async def test_rejected_source_suffix_stops_browser_without_replacing_verified_history(
    thread_browser: ThreadBrowser, fault: str, reason: str
) -> None:
    page, source = thread_browser.page, thread_browser.source
    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    await expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_enabled()

    # Corrupt the controlled upstream entry before the next event-loop yield can publish it.
    rejected = source.append(
        event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="test-browser-item", text=" INVALID SUFFIX"))
    )
    if fault == "gap":
        rejected.cursor = 6
        rejected.origin.sequence = 6
    else:
        rejected.origin.source_id = "test-conflicting-runner-source"

    await expect(page.get_by_role("alert")).to_contain_text(reason)
    await expect(page.get_by_role("alert")).to_contain_text("Showing verified history through event 4")
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_have_count(1)
    await expect(page.get_by_text("INVALID SUFFIX", exact=False)).to_have_count(0)
    await expect(page.get_by_role("combobox", name="Model", exact=True)).to_be_disabled()
    await expect(page.get_by_placeholder("Enter sends, Ctrl+Enter for a new line")).to_be_disabled()
    await expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_disabled()
    (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
    assert await thread_browser.store.events(thread.id, limit=100) == source.entries[:4]


if __name__ == "__main__":
    pytest_bazel.main()
