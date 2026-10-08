"""The built SPA consumes PostgreSQL/Electric through real HTTP/2 in Chromium.

Only the upstream runner protocol source and Kubernetes/auth boundaries are controlled. Browser
fetch, EventSource, rendering, and page reload are not replaced by the visual harness's mocks.
"""

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
import pytest_bazel
from google.protobuf import json_format
from playwright.async_api import (
    APIResponse,
    Locator,
    Page,
    Request,
    Route,
    TimeoutError as PlaywrightTimeoutError,
    expect,
)
from sqlalchemy import select, update

from agentplane.app.database import connect
from agentplane.app.testing import history_trace
from agentplane.app.testing.electric_service import ElectricService, electric_service
from agentplane.app.testing.http2_proxy import BrowserCertificate, http2_proxy
from agentplane.app.testing.replication_process import app_process
from agentplane.app.testing.replication_source import SANDBOX, SESSION, ReplicationSource
from agentplane.app.testing.thread_browser import (
    ThreadBrowser,
    append_items,
    capture_reading_anchor,
    expect_projected_cursor,
    expect_reading_anchor,
    frames,
    message_composer,
)
from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.ingestion import Ingestion
from agentplane.app.threads.models import (
    FeedState,
    ThreadCheckpoint,
    ThreadEntity,
    ThreadEvidence,
    ThreadNativeLink,
    ThreadPayloadChunk,
    ThreadPayloadManifest,
)
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import ContentStore
from agentplane.app.threads.view.views import ThreadFeedErrorState, ThreadOperationalState, ThreadViewState
from agentplane.protocol import command_pb2, event_log_pb2, event_pb2
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.viewports import DESKTOP, MOBILE, SMALL_MOBILE, Viewport
from util.testing.visual_review import upsert_review_asset
from util.visual_review import VisualReviewAsset

# gazelle:include_dep @pypi//protobuf

pytest_plugins = ("agentplane.app.testing.thread_browser",)


async def _review_screenshot(page: Page, name: str, label: str) -> None:
    output_dir = undeclared_outputs_dir()
    await page.screenshot(path=output_dir / name)
    upsert_review_asset(output_dir, title="Agentplane Thread browser", asset=VisualReviewAsset(path=name, label=label))


@pytest.fixture
async def db_url(electric: ElectricService) -> str:
    return electric.database_url


async def test_archived_thread_page_survives_deleted_sandbox_and_reload(
    page: Page,
    db_url: str,
    store: ThreadStore,
    event_logs: EventLogStore,
    ingestion: Ingestion,
    thread_source: ReplicationSource,
    electric: ElectricService,
    certificate: BrowserCertificate,
) -> None:
    thread_id = await event_logs.open(SANDBOX, SESSION, thread_source.attached.spec)
    lease = await ingestion.acquire(SANDBOX, timedelta(minutes=1))
    assert lease is not None
    await ingestion.set_attached(thread_id, thread_source.attached, lease=lease)
    await ingestion.record(thread_id, thread_source.entries, lease=lease)
    await store.rename(thread_id, "Test archived thread")
    await ingestion.release(lease)
    directory = get_required_path("_main/agentplane/app/frontend/dist/index.html").parent
    async with (
        app_process(
            db_url, runner_port=0, frontend_directory=directory, sandbox_present=False, electric_url=electric.url
        ) as app,
        http2_proxy(app.url, certificate) as ingress,
    ):
        url = f"{ingress.url}/#/threads/{thread_id}"
        await page.goto(url)
        await expect(page.get_by_role("textbox", name="Thread name", exact=True)).to_have_value("Test archived thread")
        await expect(page.get_by_text("Test retained prefix", exact=True)).to_have_count(1)
        await expect(
            page.get_by_text(
                "Sandbox no longer exists. Showing archived Thread history; controls are disabled.", exact=True
            )
        ).to_be_visible()
        await expect(message_composer(page)).to_be_disabled()
        await expect(page.get_by_role("combobox", name="Model", exact=True)).to_be_disabled()
        await expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_disabled()
        await expect(page.get_by_role("img", name="Streaming", exact=True)).to_have_count(0)
        await expect(page.get_by_role("img", name="Incomplete", exact=True)).to_have_count(1)
        await page.reload()
        await expect(page).to_have_url(url)
        await expect(page.get_by_text("Test retained prefix", exact=True)).to_have_count(1)
        await expect(
            page.get_by_text(
                "Sandbox no longer exists. Showing archived Thread history; controls are disabled.", exact=True
            )
        ).to_be_visible()
        await _review_screenshot(page, "thread-archived.png", "Archived Thread after reload")
        assert await event_logs.events(thread_id, limit=100) == thread_source.entries


async def _seed_navigation_threads(
    event_logs: EventLogStore, ingestion: Ingestion, store: ThreadStore
) -> tuple[list[UUID], list[ReplicationSource]]:
    """Two threads of 130 messages, "Thread N message M", named "Test navigation thread N"."""
    lease = await ingestion.acquire(SANDBOX, timedelta(minutes=1))
    assert lease is not None
    threads: list[UUID] = []
    sources: list[ReplicationSource] = []
    for number in range(2):
        source = ReplicationSource()
        source.attached.session_id = f"test-navigation-session-{number}"
        source.append(event_pb2.Event(harness_started=event_pb2.HarnessStarted(pid=123)))
        source.append(event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="test-navigation-turn")))
        for index in range(130):
            item_id = f"test-navigation-item-{index}"
            source.append(
                event_pb2.Event(
                    item_started=event_pb2.ItemStarted(item_id=item_id, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
                )
            )
            source.append(
                event_pb2.Event(
                    item_completed=event_pb2.ItemCompleted(item_id=item_id, text=f"Thread {number} message {index}")
                )
            )
        thread = await event_logs.open(SANDBOX, source.attached.session_id, source.attached.spec)
        await ingestion.set_attached(thread, source.attached, lease=lease)
        await ingestion.record(thread, source.entries, lease=lease)
        await store.rename(thread, f"Test navigation thread {number}")
        threads.append(thread)
        sources.append(source)
    await ingestion.release(lease)
    return threads, sources


async def test_switching_threads_starts_at_each_threads_tail(
    page: Page,
    db_url: str,
    store: ThreadStore,
    event_logs: EventLogStore,
    ingestion: Ingestion,
    electric: ElectricService,
    certificate: BrowserCertificate,
) -> None:
    threads, _ = await _seed_navigation_threads(event_logs, ingestion, store)
    directory = get_required_path("_main/agentplane/app/frontend/dist/index.html").parent
    async with (
        app_process(
            db_url, runner_port=0, frontend_directory=directory, sandbox_present=False, electric_url=electric.url
        ) as app,
        http2_proxy(app.url, certificate) as ingress,
    ):
        await page.goto(f"{ingress.url}/#/threads/{threads[0]}")
        await expect(page.get_by_text("Thread 0 message 129", exact=True)).to_be_visible()
        await page.get_by_role("region", name="Thread history", exact=True).hover()
        # The eager initial load holds message 40 (well up from the tail) but not the thread's start;
        # scrolling all the way up lands at the top of what's already loaded -- mounting message 40 --
        # and, being within a screen of that top, triggers the fetch for the page before it.
        async with page.expect_request(
            lambda request: request.method == "POST" and "entity_index < $1" in (request.post_data or "")
        ):
            await page.mouse.wheel(0, -10_000)
        await expect(page.get_by_text("Thread 0 message 40", exact=True)).to_be_visible()
        for number in (1, 0):
            async with page.expect_request(f"**/threads/{threads[number]}/sync/scope"):
                await page.locator(".agentplane-sidebar-row-name", has_text=f"Test navigation thread {number}").click()
            await expect(page.get_by_role("textbox", name="Thread name", exact=True)).to_have_value(
                f"Test navigation thread {number}"
            )
            await expect(page.get_by_text(f"Thread {number} message 129", exact=True)).to_be_visible()
        await page.screenshot(path=undeclared_outputs_dir() / "thread-navigation.png")


async def test_returning_to_a_thread_lays_its_rows_out_at_the_heights_they_had(
    page: Page,
    db_url: str,
    store: ThreadStore,
    event_logs: EventLogStore,
    ingestion: Ingestion,
    electric: ElectricService,
    certificate: BrowserCertificate,
) -> None:
    """The history lays an unmeasured row out at a flat guess; one it has read before, at the height
    it read. Rows read on a thread's first visit are laid out at those heights on the return."""
    threads, _ = await _seed_navigation_threads(event_logs, ingestion, store)
    directory = get_required_path("_main/agentplane/app/frontend/dist/index.html").parent
    async with (
        app_process(
            db_url, runner_port=0, frontend_directory=directory, sandbox_present=False, electric_url=electric.url
        ) as app,
        http2_proxy(app.url, certificate) as ingress,
    ):
        await page.goto(f"{ingress.url}/#/threads/{threads[0]}")
        await expect(page.get_by_text("Thread 0 message 129", exact=True)).to_be_visible(timeout=30_000)
        for number in (1, 0):
            await page.locator(".agentplane-sidebar-row-name", has_text=f"Test navigation thread {number}").click()
            await expect(page.get_by_text(f"Thread {number} message 129", exact=True)).to_be_visible(timeout=30_000)
        remembered = [entry.error for entry in await history_trace.estimate_errors(page) if entry.remembered]
        assert remembered, "no row on the return was laid out from what the first visit read"
        assert max(abs(error) for error in remembered) <= 2, remembered


def _older_page_bound(request: Request) -> int | None:
    """The `entity_index` a read of the page before the oldest row held is bounded by; None for any other request."""
    body = request.post_data or ""
    if request.method != "POST" or "entity_index < $1" not in body:
        return None
    return int(json.loads(body)["params"]["1"])


def _entity_handles(requests: list[Request]) -> set[str]:
    return {
        handle
        for request in requests
        if "/sync/entities?" in request.url
        for handle in parse_qs(urlsplit(request.url).query).get("handle", [])
    }


def _bodies_read(requests: list[Request]) -> int:
    """How many bodies the requests' reads of payload chunks named."""
    return sum(
        len(json.loads(request.post_data or "{}")["params"]) // 2
        for request in requests
        if request.method == "POST" and "/sync/chunks/" in request.url
    )


async def test_returning_to_a_thread_reads_only_what_changed_while_the_reader_was_away(
    page: Page,
    db_url: str,
    store: ThreadStore,
    event_logs: EventLogStore,
    ingestion: Ingestion,
    electric: ElectricService,
    certificate: BrowserCertificate,
) -> None:
    threads, sources = await _seed_navigation_threads(event_logs, ingestion, store)
    directory = get_required_path("_main/agentplane/app/frontend/dist/index.html").parent
    async with (
        app_process(
            db_url, runner_port=0, frontend_directory=directory, sandbox_present=False, electric_url=electric.url
        ) as app,
        http2_proxy(app.url, certificate) as ingress,
    ):
        requests: list[Request] = []
        chunks_read = asyncio.Event()

        def of(thread: UUID, since: int = 0) -> list[Request]:
            return [request for request in requests[since:] if f"/threads/{thread}/sync/" in request.url]

        # Playwright cannot register a builtin method such as `list.append` as a handler.
        def record(request: Request) -> None:
            requests.append(request)
            if "/sync/chunks/" in request.url:
                chunks_read.set()

        page.on("request", record)

        async def open_thread(number: int) -> None:
            async with page.expect_request(f"**/threads/{threads[number]}/sync/scope"):
                await page.locator(".agentplane-sidebar-row-name", has_text=f"Test navigation thread {number}").click()

        history = page.get_by_role("region", name="Thread history", exact=True)
        # A short viewport mounts few of the window's rows, whose bodies a reader's view reads on mounting.
        await page.set_viewport_size({**DESKTOP.size, "height": 300})
        await page.goto(f"{ingress.url}/#/threads/{threads[0]}")
        await expect(page.get_by_text("Thread 0 message 129", exact=True)).to_be_visible()
        # Before the reader scrolls: the window reads the bodies of every row it holds (its 90 rows hold 89
        # messages), not only those the view mounted.
        async with asyncio.timeout(30):
            while _bodies_read(of(threads[0])) < 60:
                await chunks_read.wait()
                chunks_read.clear()
        await page.set_viewport_size(DESKTOP.size)
        await history.hover()
        async with page.expect_request(lambda request: _older_page_bound(request) is not None):
            await page.mouse.wheel(0, -10_000)
        await expect(page.get_by_text("Thread 0 message 40", exact=True)).to_be_visible()
        first_visit = of(threads[0])
        held_down_to = min(bound for request in first_visit if (bound := _older_page_bound(request)) is not None)
        (handle,) = _entity_handles(first_visit)

        await open_thread(1)
        await expect(page.get_by_text("Thread 1 message 129", exact=True)).to_be_visible()
        # A message lands in the thread while the reader is elsewhere.
        lease = await ingestion.acquire(SANDBOX, timedelta(minutes=1))
        assert lease is not None
        source = sources[0]
        source.append(
            event_pb2.Event(
                item_started=event_pb2.ItemStarted(
                    item_id="test-navigation-item-away", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT
                )
            )
        )
        source.append(
            event_pb2.Event(
                item_completed=event_pb2.ItemCompleted(
                    item_id="test-navigation-item-away", text="Thread 0 message while away"
                )
            )
        )
        await ingestion.record(threads[0], source.entries[-2:], lease=lease)
        await ingestion.release(lease)

        mark = len(requests)
        await open_thread(0)
        await expect(page.get_by_text("Thread 0 message while away", exact=True)).to_be_visible(timeout=30_000)
        await page.screenshot(path=undeclared_outputs_dir() / "thread-returned-to.png")
        returned = of(threads[0], mark)
        # No row is read again: the thread's log is followed on from where the reader left it.
        assert not [request for request in returned if request.method == "POST" and "/sync/entities?" in request.url]
        (read, *_) = [request for request in returned if "/sync/entities?" in request.url]
        query = parse_qs(urlsplit(read.url).query)
        assert (query["handle"], query["offset"][0] not in {"now", "-1"}) == ([handle], True)
        # Nor is a body already held: the one read is the message that arrived.
        assert _bodies_read(returned) <= 1

        # The pages loaded before the reader left are still held: the next one is the page before them.
        await history.hover()
        async with page.expect_request(lambda request: _older_page_bound(request) is not None) as older:
            await page.mouse.wheel(0, -10_000)
        next_page = _older_page_bound(await older.value)
        assert next_page is not None
        assert next_page < held_down_to


async def test_projection_epoch_replacement_retires_old_requests_and_preserves_draft(
    thread_browser: ThreadBrowser,
) -> None:
    page, store, source = thread_browser.page, thread_browser.store, thread_browser.source
    thread = await thread_browser.event_logs.open(SANDBOX, SESSION, source.attached.spec)
    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    draft = message_composer(page)
    await draft.fill("Draft survives projection replacement")
    previous = await page.request.get(f"{thread_browser.ingress.url}/threads/{thread}/sync/scope")
    assert previous.ok
    old_scope = await previous.json()
    ready = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    def rebuilt_reference(reference: dict[str, object] | None) -> dict[str, object] | None:
        return {**reference, "projection_epoch": "test-rebuilt-epoch"} if reference is not None else None

    async def hold_old_evidence(route: Route) -> None:
        response = await route.fetch()
        assert response.ok
        ready.set()
        await release.wait()
        try:
            await route.fulfill(response=response)
        finally:
            finished.set()

    await page.route("**/evidence?*", hold_old_evidence)
    try:
        await click_evidence(page.locator('[data-thread-anchor="3"]'))
        async with asyncio.timeout(15):
            await ready.wait()

        # Stand in for an explicit background rebuild's atomic epoch publication.
        # This touches only the disposable test database; the live app and Electric
        # must detect replacement without a page reload or a custom client reset.
        async with store._sessions() as session, session.begin():
            rows = list(await session.scalars(select(ThreadEntity).where(ThreadEntity.thread_id == thread)))
            for row in rows:
                row.projection_epoch = "test-rebuilt-epoch"
                row.text_ref = rebuilt_reference(row.text_ref)
                row.arguments_ref = rebuilt_reference(row.arguments_ref)
                row.output_ref = rebuilt_reference(row.output_ref)
                row.input_ref = rebuilt_reference(row.input_ref)
            for model in (
                ThreadCheckpoint,
                ThreadPayloadManifest,
                ThreadPayloadChunk,
                ThreadEvidence,
                ThreadNativeLink,
            ):
                await session.execute(
                    update(model).where(model.thread_id == thread).values(projection_epoch="test-rebuilt-epoch")
                )
            await session.execute(
                update(ThreadPayloadChunk)
                .where(ThreadPayloadChunk.thread_id == thread, ThreadPayloadChunk.owner_id == "test-browser-item")
                .values(text="Test replaced prefix")
            )

        await expect(page.get_by_text("Test replaced prefix", exact=True)).to_be_visible(timeout=20_000)
        await expect(page.get_by_text("Test retained prefix", exact=True)).to_have_count(0)
        await expect(draft).to_have_value("Draft survives projection replacement")
        await expect(
            page.locator('[data-thread-anchor="3"] .agentplane-disclosure-summary[aria-expanded="true"]')
        ).to_have_count(0)
        release.set()
        async with asyncio.timeout(15):
            await finished.wait()
        await expect(
            page.locator('[data-thread-anchor="3"] .agentplane-disclosure-summary[aria-expanded="true"]')
        ).to_have_count(0)
        await expect(page.get_by_text("Test replaced prefix", exact=True)).to_be_visible()

        stale = await page.request.get(
            f"{thread_browser.ingress.url}/threads/{thread}/sync/entities",
            params={"projection_epoch": old_scope["projection_epoch"], "offset": "now"},
        )
        assert stale.status == 410
        await page.screenshot(path=undeclared_outputs_dir() / "projected-epoch-replacement.png")
    finally:
        release.set()
        if ready.is_set():
            async with asyncio.timeout(15):
                await finished.wait()
        await page.unroute("**/evidence?*", hold_old_evidence)


async def test_projected_browser_streams_runner_events_and_loads_evidence_lazily(
    page: Page, certificate: BrowserCertificate
) -> None:
    source = ReplicationSource()
    source.attached.active_turn_id = "test-projected-turn"
    source.append(event_pb2.Event(harness_started=event_pb2.HarnessStarted(pid=123)))
    source.append(event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="test-projected-turn")))
    first = source.append(
        event_pb2.Event(item_started=event_pb2.ItemStarted(item_id="first", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT))
    )
    native = source.append(
        event_pb2.Event(
            native=event_pb2.Native(
                direction=event_pb2.DIRECTION_FROM_HARNESS,
                line=json.dumps({"type": "test-text-delta", "text": "Projected browser prefix"}),
            )
        )
    )
    observed = source.append(
        event_pb2.Event(
            source_sequences=[native.cursor],
            text_delta=event_pb2.TextDelta(item_id="first", text="Projected browser prefix"),
        )
    )
    source.append(
        event_pb2.Event(item_started=event_pb2.ItemStarted(item_id="second", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT))
    )
    source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="second", text="A newer browser item")))
    reasoning = source.append(
        event_pb2.Event(item_started=event_pb2.ItemStarted(item_id="reasoning", kind=event_pb2.ITEM_KIND_REASONING))
    )
    reasoning_body = "On-demand reasoning **reaches** past one line. " * 12
    source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="reasoning", text=reasoning_body)))
    tool = source.append(
        event_pb2.Event(
            item_started=event_pb2.ItemStarted(
                item_id="tool", kind=event_pb2.ITEM_KIND_TOOL_CALL, tool_name="test-tool"
            )
        )
    )
    source.append(event_pb2.Event(tool_arguments_delta=event_pb2.ToolArgumentsDelta(item_id="tool", partial_json="{")))
    requests: list[str] = []
    body_reads: list[str] = []
    closed_disclosures_read = asyncio.Event()

    def observe(request: Request) -> None:
        requests.append(request.url)
        if request.method == "POST" and "/sync/chunks/" in request.url:
            body_reads.append(request.post_data or "")
            if all(any(f'"{owner}"' in read for read in body_reads) for owner in ("reasoning", "tool")):
                closed_disclosures_read.set()

    page.on("request", observe)
    directory = get_required_path("_main/agentplane/app/frontend/dist/index.html").parent
    async with electric_service() as service:
        engine = connect(service.database_url)
        event_logs, content = EventLogStore(engine), ContentStore(engine)
        try:
            thread = await event_logs.open(SANDBOX, SESSION, source.attached.spec)
            await ThreadStore(engine).rename(thread, "Streamed thread")
            async with (
                source.serve() as runner_port,
                app_process(
                    service.database_url, runner_port, frontend_directory=directory, electric_url=service.url
                ) as app,
                http2_proxy(app.url, certificate) as ingress,
                asyncio.timeout(60),
            ):
                opened = await source.opened.get()
                opened.replay.set()
                await page.goto(f"{ingress.url}/#/threads/{thread}")
                await expect_projected_cursor(page, source.entries[-1].cursor)
                await expect(page.get_by_text("Projected browser prefix", exact=True)).to_be_visible()
                await expect(page.get_by_text("A newer browser item", exact=True)).to_be_visible()
                # The window reads the bodies of closed disclosures ahead of any opening, and shows none.
                await closed_disclosures_read.wait()
                await expect(
                    page.locator(f'[data-thread-anchor="{reasoning.cursor}"] .agentplane-step-details')
                ).to_have_count(0)
                await expect(page.get_by_text("On-demand tool output", exact=True)).to_have_count(0)

                source.append(
                    event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="first", text=" and streamed suffix"))
                )
                await expect(
                    page.get_by_text("Projected browser prefix and streamed suffix", exact=True)
                ).to_be_visible()
                assert not any("/evidence" in url for url in requests)
                first_card = page.locator(f'[data-thread-anchor="{first.cursor}"]')
                await click_evidence(first_card)
                frame_summary = first_card.locator(
                    ".agentplane-disclosure-summary", has_text=f"Observation {observed.cursor} raw frames"
                )
                await expect(frame_summary).to_be_visible()
                # The label shares the disclosure marker's line instead of starting below it.
                summary_box = await frame_summary.bounding_box()
                label_box = await frame_summary.locator(".agentplane-disclosure-summary-content").bounding_box()
                assert summary_box is not None
                assert label_box is not None
                assert (
                    summary_box["y"] <= label_box["y"] <= summary_box["y"] + summary_box["height"] - label_box["height"]
                )
                assert not any("/frames?" in url for url in requests)
                await frame_summary.click()
                frame = first_card.locator(".agentplane-code-block")
                await expect(frame).to_contain_text("test-text-delta")
                assert json_format.Parse(await frame.inner_text(), event_log_pb2.EventEntry()) == native
                await click_evidence(first_card)
                await expect(frame).to_have_count(0)
                await click_evidence(first_card)
                await expect(frame).to_contain_text("test-text-delta")
                assert json_format.Parse(await frame.inner_text(), event_log_pb2.EventEntry()) == native
                await page.screenshot(path=undeclared_outputs_dir() / "projected-evidence-reopened.png")
                await click_evidence(first_card)
                # The reasoning step and the tool call after it are one folded run, anchored at its first step.
                run = page.locator(f'[data-thread-anchor="{reasoning.cursor}"]')
                await expect(page.locator(f'[data-thread-anchor="{tool.cursor}"]')).to_have_count(0)
                await run.get_by_text("1 tool call, 1 reasoning step", exact=True).click()
                # A tool call is one line of its arguments; opened, it shows them as they stream in.
                tool_line = run.locator(".agentplane-step-details", has_text="test-tool")
                await tool_line.locator(".agentplane-disclosure-summary").click()
                arguments = tool_line.locator(".agentplane-code-block")
                await expect(arguments.get_by_text("{", exact=True)).to_be_visible()
                source.append(
                    event_pb2.Event(
                        tool_arguments_delta=event_pb2.ToolArgumentsDelta(item_id="tool", partial_json='"path":')
                    )
                )
                await expect(arguments.get_by_text('{"path":', exact=True)).to_be_visible()
                source.append(
                    event_pb2.Event(
                        tool_arguments_delta=event_pb2.ToolArgumentsDelta(item_id="tool", partial_json='"value"}')
                    )
                )
                await expect(arguments.get_by_text('{"path":"value"}', exact=True)).to_be_visible()
                source.append(
                    event_pb2.Event(
                        item_completed=event_pb2.ItemCompleted(
                            item_id="tool", tool=event_pb2.ToolResult(output="On-demand tool output", succeeded=True)
                        )
                    )
                )
                await expect(tool_line.get_by_text("On-demand tool output", exact=True)).to_be_visible()
                reasoning_details = run.locator(".agentplane-step-details", has_text="Reasoning")
                await expect(reasoning_details).to_be_visible()
                await reasoning_details.locator(".agentplane-disclosure-summary").click()
                expanded_reasoning = reasoning_details.locator(".agentplane-disclosure-panel .agentplane-markdown")
                await expect(expanded_reasoning).to_contain_text("On-demand reasoning reaches past one line.")
                await expect(expanded_reasoning.locator("strong").first).to_have_text("reaches")
                await _review_screenshot(
                    page, "thread-streamed-expanded.png", "Streamed Thread with tool call and reasoning open"
                )

                await page.reload()
                await expect(
                    page.get_by_text("Projected browser prefix and streamed suffix", exact=True)
                ).to_have_count(1)
                assert not any(urlsplit(url).path.startswith(f"/threads/{thread}/events") for url in requests)
                assert await page.evaluate(
                    """() => performance.getEntriesByType('resource').some(
                        entry => entry.name.includes('/sync/entities?') && entry.nextHopProtocol === 'h2')"""
                )
                await page.screenshot(path=undeclared_outputs_dir() / "projected-thread-reloaded.png")
                reads_before_disconnect = len(body_reads)
                await page.context.set_offline(True)
                await ingress.drop_connections()
                source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="first", text=" after reconnect")))
                async with asyncio.timeout(10):
                    while True:
                        scope = await content.current_scope(thread)
                        if scope is not None and scope.through_cursor >= source.entries[-1].cursor:
                            break
                        await asyncio.sleep(0.01)
                # Whether or not the delta beat the drop, nothing shown is withdrawn.
                await expect(
                    page.get_by_text("Projected browser prefix and streamed suffix", exact=False)
                ).to_have_count(1)
                await page.context.set_offline(False)
                await expect(
                    page.get_by_text("Projected browser prefix and streamed suffix after reconnect", exact=True)
                ).to_have_count(1)
                # The delta arrives on the live log; the body is not read again.
                assert len(body_reads) == reads_before_disconnect
                await page.screenshot(path=undeclared_outputs_dir() / "projected-thread-reconnected.png")
        finally:
            await engine.dispose()


@pytest.mark.parametrize(
    ("viewport", "viewport_name"), [(DESKTOP, "desktop"), (MOBILE, "phone")], ids=["desktop", "phone"]
)
async def test_chronological_debug_is_lazy_paged_and_keeps_the_thread(
    thread_browser: ThreadBrowser, viewport_name: str
) -> None:
    page, source = thread_browser.page, thread_browser.source
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    for index in range(65):
        source.append(event_pb2.Event(native=event_pb2.Native(line=f"Unlinked packet {index}")))
    stderr = source.append(event_pb2.Event(harness_stderr=event_pb2.HarnessStderr(text="Debug stderr retained")))
    checkpoint = source.append(event_pb2.Event(debug_checkpoint=event_pb2.DebugCheckpoint(name="Debug checkpoint")))
    last = source.append(
        event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="test-browser-item", text=" and debug ready"))
    )
    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix and debug ready", exact=True)).to_be_visible()
    draft = message_composer(page)
    await draft.fill("Draft survives debug inspection")
    assert not any("/observations" in url for url in requests)
    await open_debug_history(page)
    dialog = page.get_by_role("dialog", name="Chronological debug")
    observations = dialog.locator("[data-debug-observation]")
    await expect(observations).to_have_count(30)
    await expect(observations.first).to_have_attribute("data-debug-observation", str(last.cursor - 29))
    await expect(observations.last).to_have_attribute("data-debug-observation", str(last.cursor))
    # The listing carries identity only: a page of 30 raw entries is what used to make opening this
    # drawer wait on its own transfer and parse, and none of them is what the reader asked to see.
    assert await dialog.locator("pre").count() == 0
    assert not any(re.search(r"/observations/\d+", url) for url in requests)
    for entry in (source.entries[-4], stderr, checkpoint, last):
        record = dialog.locator(f'[data-debug-observation="{entry.cursor}"]')
        await record.locator(".agentplane-disclosure-summary").click()
        frame = record.locator(".agentplane-code-block")
        code_or_placeholder = record.locator(".agentplane-code-block-placeholder, .agentplane-code-block")
        await expect(code_or_placeholder).to_be_visible()
        # Scrolling the placeholder can race its IntersectionObserver replacing it with
        # CodeMirror. The observation card stays mounted while its code view is lazy-mounted.
        await record.scroll_into_view_if_needed()
        await expect(frame).to_be_visible()
        assert json_format.Parse(await frame.inner_text(), event_log_pb2.EventEntry()) == entry
        assert any(url.endswith(f"/observations/{entry.cursor}") for url in requests)
    await _review_screenshot(page, f"thread-debug-{viewport_name}.png", f"Chronological debug on {viewport_name}")
    await dialog.get_by_role("button", name="Older observations", exact=True).click()
    await expect(observations).to_have_count(30)
    await expect(observations.last).to_have_attribute("data-debug-observation", str(last.cursor - 30))
    await expect(dialog.locator("pre")).to_have_count(0)
    await dialog.get_by_role("button", name="Newer observations", exact=True).click()
    await expect(observations.last).to_have_attribute("data-debug-observation", str(last.cursor))
    await page.keyboard.press("Escape")
    await expect(dialog).to_have_count(0)
    await expect(page.locator("[data-debug-observation]")).to_have_count(0)
    await expect(draft).to_have_value("Draft survives debug inspection")

    # Follow the exact semantic observation back into the original archive, including packets
    # that have no item association. The context query ends at the selected observation.
    card = page.locator('[data-thread-anchor="3"]')
    await click_evidence(card)
    await card.get_by_role("button", name="Inspect chronological context").first.click()
    await expect(observations.last).to_have_attribute("data-debug-observation", "3")
    assert parse_qs(urlsplit([url for url in requests if "/observations" in url][-1]).query)["before_cursor"] == ["4"]
    await dialog.get_by_role("button", name="Latest observations", exact=True).click()
    await expect(observations.last).to_have_attribute("data-debug-observation", str(last.cursor))
    await page.keyboard.press("Escape")
    await expect(dialog).to_have_count(0)
    await expect(draft).to_have_value("Draft survives debug inspection")
    await expect(card.locator(".agentplane-evidence-toggle")).to_have_attribute("aria-expanded", "true")

    # Closing the drawer cancels an in-flight real archive response. A response released
    # afterwards must not repopulate the closed view or disturb the thread draft.
    response_ready = asyncio.Event()
    release_response = asyncio.Event()
    response_finished = asyncio.Event()

    async def hold_debug_response(route: Route) -> None:
        response = await route.fetch()
        response_ready.set()
        await release_response.wait()
        try:
            await route.fulfill(response=response)
        finally:
            response_finished.set()

    await page.route("**/observations?*", hold_debug_response)
    try:
        await open_debug_history(page)
        async with asyncio.timeout(10):
            await response_ready.wait()
        async with page.expect_event("requestfailed", predicate=lambda request: "/observations" in request.url):
            await page.keyboard.press("Escape")
            release_response.set()
        async with asyncio.timeout(10):
            await response_finished.wait()
        await expect(dialog).to_have_count(0)
        await expect(page.locator("[data-debug-observation]")).to_have_count(0)
        await expect(draft).to_have_value("Draft survives debug inspection")
    finally:
        release_response.set()
        await page.unroute("**/observations?*", hold_debug_response)


async def test_browser_replays_streams_and_reloads_one_exact_thread(thread_browser: ThreadBrowser) -> None:
    page, source = thread_browser.page, thread_browser.source
    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="test-browser-item", text=" and live suffix")))
    complete_text = "Test retained prefix and live suffix"
    await expect(page.get_by_text(complete_text, exact=True)).to_be_visible()
    body_reads: list[str] = []

    def observe(request: Request) -> None:
        if request.method == "POST" and "/sync/chunks/" in request.url:
            body_reads.append(request.post_data or "")

    page.on("request", observe)
    source.append(
        event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id="test-browser-item", text=complete_text))
    )
    # The later item's row follows the completion's on the same log, so its body is read after any
    # read the completion caused.
    source.append(
        event_pb2.Event(
            item_started=event_pb2.ItemStarted(item_id="test-browser-later", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
        )
    )
    source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="test-browser-later", text="Test later item")))
    await expect(page.get_by_text("Test later item", exact=True)).to_be_visible()
    assert any('"test-browser-later"' in read for read in body_reads)
    # Completed with the text that streamed, the body the reader holds is not read again.
    assert not any('"test-browser-item"' in read for read in body_reads)
    source.attached.active_turn_id = ""
    source.append(
        event_pb2.Event(
            turn_completed=event_pb2.TurnCompleted(turn_id="test-browser-turn", status=event_pb2.TURN_STATUS_COMPLETED)
        )
    )
    await expect(page.get_by_role("img", name="Streaming", exact=True)).to_have_count(0)
    await expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_disabled()

    # Reload replaces the JS document, including component memory and its Electric collections. Durable
    # synchronization must rebuild the same text once, not append the prefix to the live card a second time.
    await page.reload()
    await expect(page.get_by_text(complete_text, exact=True)).to_have_count(1)
    await expect(page.get_by_role("img", name="Streaming", exact=True)).to_have_count(0)
    await expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_disabled()
    (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
    assert await thread_browser.event_logs.events(thread.id, limit=100) == source.entries


async def test_sidebar_receives_rename_and_archive_from_another_app_replica(thread_browser: ThreadBrowser) -> None:
    page = thread_browser.page
    await thread_browser.start_replay()
    sidebar = page.get_by_role("navigation", name="Threads", exact=True)
    await expect(sidebar.get_by_text("Browser thread", exact=True)).to_be_visible()
    (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
    await thread_browser.store.rename(thread.id, "Test rename from another replica")
    await expect(sidebar.get_by_text("Test rename from another replica", exact=True)).to_be_visible()
    await expect(sidebar.get_by_text("Browser thread", exact=True)).to_have_count(0)
    await thread_browser.store.archive(thread.id)
    # The thread is open, so the sidebar keeps its row, now offering to unarchive it, though the archived
    # switch is off.
    unarchive = sidebar.get_by_role("button", name="Unarchive Test rename from another replica", exact=True)
    await expect(unarchive).to_be_visible()
    await expect(page.get_by_role("switch", name="Show archived threads", exact=True)).not_to_be_checked()
    await expect(page).to_have_url(f"{thread_browser.ingress.url}/#/threads/{thread.id}")
    await page.screenshot(path=undeclared_outputs_dir() / "sidebar-replica-updates.png")


@pytest.mark.parametrize("raw", [False, True], ids=["normal", "raw"])
@pytest.mark.parametrize(
    ("viewport", "resized_viewport"),
    [(DESKTOP, replace(DESKTOP, height=SMALL_MOBILE.height)), (MOBILE, SMALL_MOBILE)],
    ids=["desktop", "phone"],
)
async def test_thread_follows_bottom_until_reader_scrolls_up(
    thread_browser: ThreadBrowser,
    raw: bool,
    viewport: Viewport,
    resized_viewport: Viewport,
    request: pytest.FixtureRequest,
) -> None:
    page, source = thread_browser.page, thread_browser.source
    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    if raw:
        await expand_item_evidence(page)
    history = page.get_by_role("region", name="Thread history", exact=True)
    source.append(
        event_pb2.Event(
            item_completed=event_pb2.ItemCompleted(item_id="test-browser-item", text="Test retained prefix")
        )
    )
    for number in range(18):
        item_id = f"test-scroll-item-{number}"
        text = f"Test earlier message {number}\n\n" + "A retained paragraph for the reading viewport. " * 8
        source.append(
            event_pb2.Event(
                item_started=event_pb2.ItemStarted(item_id=item_id, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
            )
        )
        source.append(event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id=item_id, text=text)))
    source.append(
        event_pb2.Event(
            item_started=event_pb2.ItemStarted(item_id="test-scroll-tail", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
        )
    )
    source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="test-scroll-tail", text="Test tail seed")))
    await expect(page.get_by_text("Test tail seed", exact=True)).to_have_count(1)
    await page.wait_for_function(
        """() => {
            const area = document.querySelector('[aria-label="Thread history"]');
            return area.scrollHeight > area.clientHeight * 2 &&
                area.scrollHeight - area.clientHeight - area.scrollTop <= 2;
        }"""
    )

    # One existing card grows through several native deltas, without a new message or scroll.
    for number in range(5):
        source.append(
            event_pb2.Event(
                text_delta=event_pb2.TextDelta(
                    item_id="test-scroll-tail", text=f"\n\nTest streaming paragraph {number}"
                )
            )
        )
    await expect(page.get_by_text("Test streaming paragraph 4", exact=True)).to_have_count(1)
    await expect_history_bottom(page)
    await page.set_viewport_size(resized_viewport.size)
    await expect_history_bottom(page)
    await page.screenshot(path=undeclared_outputs_dir() / f"{request.node.name}-following.png")

    await history.hover()
    # The app adopts the reader's position at the gesture's scrollend. Rows entering the window can
    # still load and be re-measured after it, and the scroll correction for a re-measure lands a
    # frame after its commit; capture_reading_anchor samples once two consecutive frames agree.
    gesture = await history.evaluate_handle("area => window.__threadPage.scrollEnded(area)")
    await page.mouse.wheel(0, -600)
    async with asyncio.timeout(30):
        await gesture.evaluate("gesture => gesture.ended")
        reading_anchor = await capture_reading_anchor(history)
    await gesture.dispose()
    updated = source.append(
        event_pb2.Event(
            text_delta=event_pb2.TextDelta(item_id="test-scroll-tail", text="\n\nTest output while reading")
        )
    )
    await expect_projected_cursor(page, updated.cursor)
    source.append(
        event_pb2.Event(
            item_started=event_pb2.ItemStarted(item_id="test-scroll-next", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
        )
    )
    completed = source.append(
        event_pb2.Event(
            item_completed=event_pb2.ItemCompleted(item_id="test-scroll-next", text="Test new message while reading")
        )
    )
    await expect_projected_cursor(page, completed.cursor)
    # Wait for the paint following layout/ResizeObserver, so a premature assertion cannot miss
    # an unwanted jump scheduled by that observer. No elapsed-time delay stands in for rendering.
    await frames(page)
    await expect_reading_anchor(page, reading_anchor)
    await page.set_viewport_size({**resized_viewport.size, "height": resized_viewport.height + 50})
    await frames(page)
    await expect_reading_anchor(page, reading_anchor)
    # A late expansion above the reader can advance scrollTop through browser anchoring.
    # Passing the old bottom that way must not be mistaken for returning to it.
    previous_bottom = await history.evaluate("area => area.scrollHeight - area.clientHeight")
    await history.locator(".agentplane-markdown").first.evaluate(
        """message => {
            const area = message.closest('[aria-label="Thread history"]');
            message.style.minHeight = `${area.scrollHeight}px`;
        }"""
    )
    await frames(page)
    assert await history.evaluate("area => area.scrollTop") > previous_bottom
    await expect_reading_anchor(page, reading_anchor)
    assert await history.evaluate("area => area.scrollHeight - area.clientHeight - area.scrollTop") > 24
    await page.screenshot(path=undeclared_outputs_dir() / f"{request.node.name}-reading.png")

    # Scroll events are queued. Grow a rendered item in the same task as returning to the
    # bottom: when that event arrives, the old bottom is already behind the new content.
    # This reproduces a late layout expansion without relying on network/frame timing.
    await history.evaluate(
        """area => {
            area.scrollTo({ top: area.scrollHeight });
            const message = [...area.querySelectorAll('.agentplane-markdown')].at(-1);
            message.style.minHeight = `${message.offsetHeight + 240}px`;
        }"""
    )
    await expect_history_bottom(page)
    source.append(
        event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="test-scroll-tail", text="\n\nTest following again"))
    )
    await expect(page.get_by_text("Test following again", exact=True)).to_have_count(1)
    await expect_history_bottom(page)
    # Increasing the viewport height must keep following too, including browser scroll clamping.
    await page.set_viewport_size(viewport.size)
    await expect_history_bottom(page)
    await page.screenshot(path=undeclared_outputs_dir() / f"{request.node.name}-resumed.png")


async def test_small_upward_scroll_stays_detached_when_tail_streams(thread_browser: ThreadBrowser) -> None:
    """A streamed update during a slow, sub-slack mouse scroll must not reattach following."""
    page, source = thread_browser.page, thread_browser.source
    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    for number in range(18):
        item_id = f"slow-scroll-item-{number}"
        source.append(
            event_pb2.Event(
                item_started=event_pb2.ItemStarted(item_id=item_id, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
            )
        )
        latest = source.append(
            event_pb2.Event(
                item_completed=event_pb2.ItemCompleted(
                    item_id=item_id, text=f"Earlier message {number}. " + "A retained paragraph. " * 8
                )
            )
        )
    await expect_projected_cursor(page, latest.cursor)
    source.append(
        event_pb2.Event(
            item_started=event_pb2.ItemStarted(item_id="slow-scroll-tail", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
        )
    )
    tail = source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="slow-scroll-tail", text="Tail seed")))
    await expect_projected_cursor(page, tail.cursor)

    history = page.get_by_role("region", name="Thread history", exact=True)
    await expect_history_bottom(page)
    await frames(page)
    initial_scroll_top = await history.evaluate("area => area.scrollTop")
    gesture = await history.evaluate_handle(
        """area => {
            const state = { events: [], ended: new Promise(resolve => {
                area.addEventListener("scrollend", () => {
                    state.events.push("scrollend");
                    resolve();
                }, { once: true });
            }) };
            const observer = new MutationObserver(() => {
                if (area.textContent.includes("Small-scroll stream update")) {
                    observer.disconnect();
                    requestAnimationFrame(() => state.events.push("stream-rendered"));
                }
            });
            observer.observe(area, { subtree: true, childList: true, characterData: true });
            return state;
        }"""
    )
    bounds = await history.bounding_box()
    assert bounds is not None
    # Keep one native mouse gesture open while streaming updates, within the 24px follow slack.
    cdp = await page.context.new_cdp_session(page)
    scroll_gesture = asyncio.create_task(
        cdp.send(
            "Input.synthesizeScrollGesture",
            {
                "x": bounds["x"] + bounds["width"] / 2,
                "y": bounds["y"] + bounds["height"] / 2,
                "gestureSourceType": "mouse",
                "preventFling": True,
                "speed": 10,
                "yDistance": 20,
            },
        )
    )
    try:
        await page.wait_for_function(
            """initial => {
                const area = document.querySelector('[aria-label="Thread history"]');
                const gap = area.scrollHeight - area.scrollTop - area.clientHeight;
                return area.scrollTop < initial && gap > 0 && gap < 24;
            }""",
            arg=initial_scroll_top,
        )

        tail_markdown = history.locator(".agentplane-markdown").last
        previous_height = await tail_markdown.evaluate("message => message.getBoundingClientRect().height")
        updated = source.append(
            event_pb2.Event(
                text_delta=event_pb2.TextDelta(
                    item_id="slow-scroll-tail",
                    text="\n\nSmall-scroll stream update\n\n" + "Streaming continuation. " * 240,
                )
            )
        )
        await expect_projected_cursor(page, updated.cursor)
        await expect(page.get_by_text("Small-scroll stream update", exact=True)).to_have_count(1)
        await page.wait_for_function(
            """previous => {
                const messages = document.querySelectorAll('[aria-label="Thread history"] .agentplane-markdown');
                return messages.length > 0 && messages[messages.length - 1].getBoundingClientRect().height > previous + 24;
            }""",
            arg=previous_height,
        )
        await frames(page)
    finally:
        await scroll_gesture
        await cdp.detach()

    async with asyncio.timeout(5):
        await gesture.evaluate("state => state.ended")
    events_after_scrollend = await gesture.evaluate("state => state.events")
    assert events_after_scrollend.index("stream-rendered") < events_after_scrollend.index("scrollend"), (
        f"expected the streamed update to render before scrollend: {events_after_scrollend}"
    )
    await frames(page)
    bottom_gap = await history.evaluate("area => area.scrollHeight - area.scrollTop - area.clientHeight")
    assert bottom_gap > 24, f"streaming growth pulled the reader back to the bottom (gap={bottom_gap:.1f}px)"
    await gesture.dispose()


def append_tool_call(thread_browser: ThreadBrowser, name: str) -> event_log_pb2.EventEntry:
    """A finished `Bash` call whose command and output are each taller than their clamps."""
    item_id = f"resize-tool-{name}"
    arguments = {
        "command": "\n".join(f"echo command line {number}" for number in range(24)),
        "description": f"Run tool {name}",
    }
    source = thread_browser.source
    source.append(
        event_pb2.Event(
            item_started=event_pb2.ItemStarted(item_id=item_id, kind=event_pb2.ITEM_KIND_TOOL_CALL, tool_name="Bash")
        )
    )
    source.append(
        event_pb2.Event(tool_arguments=event_pb2.ToolArguments(item_id=item_id, arguments_json=json.dumps(arguments)))
    )
    output = "\n".join(f"output line {number}" for number in range(60))
    return source.append(
        event_pb2.Event(
            item_completed=event_pb2.ItemCompleted(
                item_id=item_id, tool=event_pb2.ToolResult(output=output, succeeded=True)
            )
        )
    )


async def append_run_among_rows(thread_browser: ThreadBrowser, *, below: int) -> None:
    """Reading rows, a run of three finished tool calls, then `below` more rows, all in the thread."""
    await thread_browser.start_replay()
    await expect(thread_browser.page.get_by_text("Test retained prefix", exact=True)).to_be_visible(timeout=30_000)
    append_items(thread_browser, "above", range(25))
    for name in ("a", "b", "c"):
        latest = append_tool_call(thread_browser, name)
    if below:
        latest = append_items(thread_browser, "below", range(below))
    await expect_projected_cursor(thread_browser.page, latest.cursor)
    # The rows are in before their text is, and each grows when its text arrives.
    await expect(thread_browser.page.get_by_role("region", name="Thread history", exact=True)).to_have_attribute(
        "data-layout-settled", "true", timeout=30_000
    )


@asynccontextmanager
async def output_streaming_in(thread_browser: ThreadBrowser) -> AsyncIterator[list[int]]:
    """A call running at the tail whose output keeps arriving, each delta once the browser has it,
    for the length of the block; yields the cursors delivered so far."""
    source, page = thread_browser.source, thread_browser.page
    source.append(
        event_pb2.Event(
            item_started=event_pb2.ItemStarted(
                item_id="resize-live", kind=event_pb2.ITEM_KIND_TOOL_CALL, tool_name="Bash"
            )
        )
    )
    source.append(
        event_pb2.Event(
            tool_arguments=event_pb2.ToolArguments(
                item_id="resize-live", arguments_json=json.dumps({"command": "tail -f /var/log/test.log"})
            )
        )
    )
    delivered: list[int] = []

    async def stream() -> None:
        while True:
            entry = source.append(
                event_pb2.Event(
                    tool_output_delta=event_pb2.ToolOutputDelta(
                        item_id="resize-live", text=f"streamed line {len(delivered)}\n"
                    )
                )
            )
            await expect_projected_cursor(page, entry.cursor)
            delivered.append(entry.cursor)

    streaming = asyncio.create_task(stream())
    try:
        yield delivered
    finally:
        streaming.cancel()
        with suppress(asyncio.CancelledError):
            await streaming


async def wheel(page: Page, delta: float) -> None:
    """One wheel gesture over the history, until it has ended and the app has adopted where it left
    the reader."""
    history = page.get_by_role("region", name="Thread history", exact=True)
    await history.hover()
    gesture = await history.evaluate_handle("area => window.__threadPage.scrollEnded(area)")
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
            f"{line} never came to rest; the history's last events:\n{await history_trace.recent(page, 40)}"
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
        f"{line} left the page; the history's last events:\n{await history_trace.recent(page, 60)}"
    )
    assert outcome["drift"] <= 2, (
        f"{line} moved {outcome['drift']}px from where it was clicked; the history's last events:\n"
        f"{await history_trace.recent(page, 60)}"
    )


def tool_call_in(run: Locator, name: str) -> tuple[Locator, Locator]:
    """A call in `run` and the card around it, whose top edge holds still as the call opens."""
    selector = ".agentplane-step-details"
    call = run.locator(selector, has_text=f"Run tool {name}")
    card = call.locator(
        "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' agentplane-evidence-owner ')][1]"
    )
    return call, card


async def test_the_history_is_not_settled_while_a_row_is_still_loading_its_text(thread_browser: ThreadBrowser) -> None:
    """A row is in before its text and grows when the text arrives, so a history that said it had
    settled in between would move its rows after saying they were at rest."""
    page = thread_browser.page
    await page.set_viewport_size(MOBILE.size)
    release = asyncio.Event()

    async def hold_text(route: Route) -> None:
        await release.wait()
        await route.continue_()

    await page.route("**/sync/chunks/**", hold_text)
    try:
        thread_browser.opened.replay.set()
        history = page.get_by_role("region", name="Thread history", exact=True)
        loading = history.get_by_text("Loading complete revision…")
        await expect(loading.first).to_be_visible(timeout=30_000)
        # Many times the frames a layout that waits for nothing needs to settle.
        for _ in range(15):
            await frames(page)
        await expect(history).to_have_attribute("data-layout-settled", "false")
        release.set()
        await expect(history).to_have_attribute("data-layout-settled", "true", timeout=30_000)
        await expect(loading).to_have_count(0)
    finally:
        release.set()
        await page.unroute("**/sync/chunks/**", hold_text)


@pytest.mark.parametrize(
    ("viewport", "viewport_name"), [(DESKTOP, "desktop"), (MOBILE, "phone")], ids=["desktop", "phone"]
)
async def test_expanded_command_stays_collapsible(thread_browser: ThreadBrowser, viewport_name: str) -> None:
    page = thread_browser.page
    await append_run_among_rows(thread_browser, below=8)
    history = page.get_by_role("region", name="Thread history", exact=True)
    run = history.locator("[data-thread-anchor]").filter(has_text="3 tool calls")
    await read_at(page, run, 0.1)
    await run.locator(".agentplane-disclosure-summary").first.click()
    call, _ = tool_call_in(run, "a")
    await call.locator(".agentplane-disclosure-summary").first.click()
    command = call.locator('.agentplane-clamped-block[data-label="Command"]')
    show_all = command.get_by_role("button", name="Show all 24 lines", exact=True)
    collapse = command.get_by_role("button", name="Command", exact=True)
    await read_at(page, show_all, 0.3)
    editor = await command.locator(".cm-editor").element_handle()
    assert editor is not None

    async def select_command_line(line_text: str) -> None:
        line = command.locator(".cm-line").filter(has_text=line_text)
        await expect(line).to_have_count(1)
        await line.evaluate(
            """line => {
                const range = document.createRange();
                range.selectNodeContents(line);
                const selection = window.getSelection();
                if (!selection) throw new Error("the document has no text selection");
                selection.removeAllRanges();
                selection.addRange(range);
            }"""
        )
        assert await page.evaluate("() => window.getSelection()?.toString()") == line_text

    for _ in range(2):
        await select_command_line("echo command line 0")
        await show_all.click()
        # Deliver layout/ResizeObserver callbacks: the old observer measured a detached node and
        # removed the collapse control after the first render of the expanded block.
        await frames(page)
        await expect(command).to_have_attribute("data-expanded", "true")
        await expect(
            command.locator(".agentplane-clamped-disclosure .agentplane-disclosure-heading button")
        ).to_have_count(1)
        assert await editor.evaluate("node => node.isConnected"), "expanding remounted the CodeMirror editor"
        assert await page.evaluate("() => window.getSelection()?.toString()") == "echo command line 0"
        await read_at(page, command.locator(".cm-line").filter(has_text="echo command line 20"), 0.5)
        await expect(collapse).to_be_in_viewport()
        await select_command_line("echo command line 20")
        await _review_screenshot(
            page, f"thread-command-expanded-{viewport_name}.png", f"Expanded command on {viewport_name}"
        )
        await collapse.click()
        await frames(page)
        assert await editor.evaluate("node => node.isConnected"), "collapsing remounted the CodeMirror editor"
        assert await page.evaluate("() => window.getSelection()?.toString()") == "echo command line 20"
        await expect(command.locator('[data-clamped="true"]')).to_have_count(1)
        await expect(show_all).to_be_visible()
        await expect(collapse).to_have_count(0)
        nested_heading_display = await command.evaluate(
            """block => {
                const panel = block.querySelector(
                    ".agentplane-clamped-disclosure .agentplane-disclosure-content"
                );
                if (!panel) throw new Error("clamped disclosure panel is missing");
                const nested = document.createElement("div");
                nested.className = "agentplane-disclosure";
                const item = document.createElement("div");
                item.className = "agentplane-disclosure-item";
                const heading = document.createElement("div");
                heading.className = "agentplane-disclosure-heading";
                item.append(heading);
                nested.append(item);
                panel.append(nested);
                const display = getComputedStyle(heading).display;
                nested.remove();
                return display;
            }"""
        )
        assert nested_heading_display == "flex", "collapsed ClampedBlock styles hid a nested disclosure heading"
        await expect(call.locator(".agentplane-disclosure-summary").first).to_have_attribute("aria-expanded", "true")
        await _review_screenshot(
            page, f"thread-command-collapsed-{viewport_name}.png", f"Collapsed command on {viewport_name}"
        )


@pytest.mark.parametrize("following", [False, True], ids=["mid-thread", "following"])
@pytest.mark.parametrize(
    ("viewport", "viewport_name"), [(DESKTOP, "desktop"), (MOBILE, "phone")], ids=["desktop", "phone"]
)
async def test_opening_a_call_and_its_output_keeps_it_collapsible_while_reading(
    thread_browser: ThreadBrowser,
    viewport: Viewport,
    viewport_name: str,
    following: bool,
    request: pytest.FixtureRequest,
) -> None:
    """Opening a run or call preserves its row; a long output stays collapsible while the reader scrolls it."""
    page = thread_browser.page
    await append_run_among_rows(thread_browser, below=8 if following else 40)
    history = page.get_by_role("region", name="Thread history", exact=True)
    run = history.locator("[data-thread-anchor]").filter(has_text="3 tool calls")
    if following:
        await expect_history_bottom(page)
    else:
        await read_at(page, run, 0.1)

    # A card gains border and padding as it opens, which moves its label; its top edge is the place.
    summary = run.locator(".agentplane-disclosure-summary").first
    async with holding_still(page, run):
        await summary.click()
        await expect(run.locator(".agentplane-step-details")).to_have_count(3)

    call, card = tool_call_in(run, "a")
    show_all = call.get_by_role("button", name="Show all 60 lines")
    async with holding_still(page, card):
        await call.locator(".agentplane-disclosure-summary").first.click()
        await expect(show_all).to_be_visible()
    await page.screenshot(path=undeclared_outputs_dir() / f"{request.node.name}-call-open.png")

    await read_at(page, show_all, 0.3)
    await show_all.click()
    collapse = call.locator(".agentplane-output-disclosure .agentplane-disclosure-summary")
    output_line = call.locator('.agentplane-clamped-block[data-label="Output"] .cm-line').filter(
        has_text="output line 45"
    )
    await wheel(page, 500)
    await expect(output_line).to_be_visible()
    await read_at(page, output_line, 0.5)
    await expect(collapse).to_be_in_viewport()
    await expect(collapse).to_have_attribute("aria-expanded", "true")
    collapse_box = await collapse.bounding_box()
    output_heading = call.locator(".agentplane-output-disclosure .agentplane-disclosure-heading")
    output_heading_box = await output_heading.bounding_box()
    call_heading_box = await call.locator(".agentplane-disclosure-heading").first.bounding_box()
    run_heading_box = await run.locator(".agentplane-disclosure-heading").first.bounding_box()
    history_box = await history.bounding_box()
    assert collapse_box is not None
    assert output_heading_box is not None
    assert call_heading_box is not None
    assert run_heading_box is not None
    assert history_box is not None
    minimum_control_height = 44 if viewport == MOBILE else 28
    assert collapse_box["height"] >= minimum_control_height, f"collapse target is too short: {collapse_box}"
    if viewport == DESKTOP:
        for heading_name, heading_box in (
            ("run", run_heading_box),
            ("tool call", call_heading_box),
            ("output", output_heading_box),
        ):
            assert heading_box["height"] <= 30, f"desktop {heading_name} heading is too tall: {heading_box}"
    divider_box = await output_heading.evaluate(
        """heading => {
          const box = heading.getBoundingClientRect();
          const divider = getComputedStyle(heading, "::after");
          return { left: box.left + parseFloat(divider.left), right: box.right - parseFloat(divider.right) };
        }"""
    )
    card_box = await card.bounding_box()
    assert card_box is not None
    assert abs(divider_box["left"] - card_box["x"]) <= 2, (
        f"the output divider should reach the card's left edge: {divider_box=} {card_box=}"
    )
    assert abs(divider_box["right"] - (card_box["x"] + card_box["width"])) <= 2, (
        f"the output divider should reach the card's right edge: {divider_box=} {card_box=}"
    )
    assert abs(run_heading_box["y"] - history_box["y"]) <= 2, (
        f"the run heading should use the history's top sticky slot: {run_heading_box=} {history_box=}"
    )
    assert abs(call_heading_box["y"] - (run_heading_box["y"] + run_heading_box["height"])) <= 2, (
        f"the tool-call heading should stack below the run heading: {call_heading_box=} {run_heading_box=}"
    )
    assert abs(output_heading_box["y"] - (call_heading_box["y"] + call_heading_box["height"])) <= 2, (
        f"the output heading should stack below the tool-call heading: {output_heading_box=} {call_heading_box=}"
    )
    assert abs(collapse_box["y"] - output_heading_box["y"]) <= 2, (
        f"the output accordion control should occupy its sticky heading row: {collapse_box=} {output_heading_box=}"
    )
    active_sticky_action = await page.evaluate(
        """point => document.elementFromPoint(point.x, point.y)?.closest('button')?.getAttribute('aria-expanded')""",
        {"x": collapse_box["x"] + collapse_box["width"] / 2, "y": collapse_box["y"] + collapse_box["height"] / 2},
    )
    assert active_sticky_action == "true", (
        f"a parent heading obscured the output Disclosure control: {active_sticky_action}"
    )
    await _review_screenshot(
        page,
        f"thread-output-{viewport_name}-{'following' if following else 'reading'}.png",
        f"Expanded tool output on {viewport_name} while {'following' if following else 'reading'}",
    )
    # At the true tail, collapsing a long block can shorten the history below its current scrollTop.
    # The browser must clamp to the new bottom; pixel-stable anchoring is asserted mid-thread, where
    # the history has room to preserve the clicked heading's position.
    if following:
        await collapse.click()
    else:
        async with holding_still(page, collapse):
            await collapse.click()
    await expect(history).to_have_attribute("data-scroll-mode", "reading")
    await expect(collapse).to_have_attribute("aria-expanded", "false")
    await expect(output_line).to_be_hidden()
    await expect(call.locator(".agentplane-disclosure-summary").first).to_have_attribute("aria-expanded", "true")
    await expect(run.locator(".agentplane-disclosure-summary").first).to_have_attribute("aria-expanded", "true")
    for heading in (
        run.locator(".agentplane-disclosure-heading").first,
        call.locator(".agentplane-disclosure-heading").first,
        call.locator(".agentplane-output-disclosure .agentplane-disclosure-heading"),
    ):
        await expect(heading).to_be_in_viewport()
    await page.screenshot(path=undeclared_outputs_dir() / f"{request.node.name}-output-collapsed.png")


async def test_opening_a_call_while_output_streams_in_keeps_it_collapsible(thread_browser: ThreadBrowser) -> None:
    page = thread_browser.page
    await append_run_among_rows(thread_browser, below=40)
    history = page.get_by_role("region", name="Thread history", exact=True)
    run = history.locator("[data-thread-anchor]").filter(has_text="3 tool calls")
    await read_at(page, run, 0.1)

    call, card = tool_call_in(run, "a")
    show_all = call.get_by_role("button", name="Show all 60 lines")
    async with output_streaming_in(thread_browser) as delivered:
        async with holding_still(page, run, rest_first=False):
            await run.locator(".agentplane-disclosure-summary").first.click()
            await expect(call).to_be_visible()
        async with holding_still(page, card, rest_first=False):
            await call.locator(".agentplane-disclosure-summary").first.click()
            await expect(show_all).to_be_visible()
        await read_at(page, show_all, 0.3)
        await show_all.click()
        collapse = call.locator(".agentplane-output-disclosure .agentplane-disclosure-summary")
        output_line = call.locator('.agentplane-clamped-block[data-label="Output"] .cm-line').filter(
            has_text="output line 45"
        )
        await wheel(page, 500)
        await expect(output_line).to_be_visible()
        await read_at(page, output_line, 0.5)
        await expect(collapse).to_be_in_viewport()
        await collapse.click()
        await expect(collapse).to_have_attribute("aria-expanded", "false")
    assert len(delivered) >= 3, "the tail's output was not arriving while the call was opened"


@pytest.mark.parametrize("leaving", ["opening a row", "scrolling up"])
@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "phone"])
async def test_a_reader_away_from_the_end_of_a_live_thread_can_jump_back_to_it(
    thread_browser: ThreadBrowser, leaving: str, request: pytest.FixtureRequest
) -> None:
    """The thread is running. A reader who has left its end, by scrolling up or by opening a row that
    grew past the view, is shown that they are not following it; one click returns to the end, and
    following resumes."""
    page = thread_browser.page
    await append_run_among_rows(thread_browser, below=8)
    history = page.get_by_role("region", name="Thread history", exact=True)
    jump = page.get_by_role("button", name="Jump to latest")
    await expect_history_bottom(page)
    await expect(jump).to_have_count(0)

    if leaving == "opening a row":
        await (
            history.locator("[data-thread-anchor]")
            .filter(has_text="3 tool calls")
            .locator(".agentplane-disclosure-summary")
            .first.click()
        )
    else:
        await wheel(page, -1500)
    await expect(jump).to_be_visible()
    # Events keep arriving; the reader is not carried along.
    latest = append_items(thread_browser, "arriving", range(3))
    await expect_projected_cursor(page, latest.cursor)
    assert await history.evaluate("area => area.scrollHeight - area.clientHeight - area.scrollTop") > 24
    await page.screenshot(path=undeclared_outputs_dir() / f"{request.node.name}-away.png")

    await jump.click()
    await expect_history_bottom(page)
    await expect(jump).to_have_count(0)
    latest = append_items(thread_browser, "following-again", range(3, 6))
    await expect_projected_cursor(page, latest.cursor)
    await expect_history_bottom(page)


async def expect_archived_events(
    event_logs: EventLogStore, thread_id: UUID, expected: list[event_log_pb2.EventEntry]
) -> list[event_log_pb2.EventEntry]:
    async with asyncio.timeout(15):
        while True:
            events = await event_logs.events(thread_id, limit=100)
            if events == expected:
                return events
            await asyncio.sleep(0.01)


async def expect_history_bottom(page: Page) -> None:
    try:
        await page.wait_for_function(
            """() => {
                const area = document.querySelector('[aria-label="Thread history"]');
                return area.scrollHeight - area.clientHeight - area.scrollTop <= 2;
            }"""
        )
    except PlaywrightTimeoutError:
        raise AssertionError(
            f"the history never reached its bottom; its last events:\n{await history_trace.recent(page, 60)}"
        ) from None


@pytest.mark.parametrize(("viewport", "raw"), [(DESKTOP, False), (MOBILE, True)], ids=["desktop-normal", "phone-raw"])
async def test_failed_turn_preserves_confirmed_input_and_allows_another_turn(
    thread_browser: ThreadBrowser, raw: bool, request: pytest.FixtureRequest
) -> None:
    page, source = thread_browser.page, thread_browser.source
    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    if raw:
        await expand_item_evidence(page)
    composer = message_composer(page)
    await composer.fill("Test input confirmed before a model error")
    await composer.press("Enter")
    async with asyncio.timeout(15):
        command = await source.commands.get()
    source.append(
        event_pb2.Event(
            harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
                harness_message_id="test-error-input",
                origin_command_ids=[command.command_id],
                text=command.submit_input.text,
                turn_id="test-browser-turn",
            )
        )
    )
    partial = "".join(f"\n\nTest partial paragraph {number}" for number in range(30))
    source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="test-browser-item", text=partial)))
    await expect(page.get_by_text("Test partial paragraph 29", exact=True)).to_have_count(1)
    await expect_history_bottom(page)
    diagnostic = "Test model request failed: HTTP 429\n<img src=x onerror=\"throw new Error('unsafe diagnostic')\">"
    native = source.append(
        event_pb2.Event(
            native=event_pb2.Native(
                direction=event_pb2.DIRECTION_FROM_HARNESS, line=json.dumps({"type": "error", "message": diagnostic})
            )
        )
    )
    source.attached.active_turn_id = ""
    failed = source.append(
        event_pb2.Event(
            source_sequences=[native.cursor],
            turn_completed=event_pb2.TurnCompleted(
                turn_id="test-browser-turn", status=event_pb2.TURN_STATUS_FAILED, error=diagnostic
            ),
        )
    )
    # The label leads the diagnostic in one paragraph, so the alert is what holds the diagnostic's text.
    error_text = page.get_by_role("alert").filter(has_text=diagnostic)
    await expect(error_text).to_have_count(1)
    await expect(error_text).to_be_in_viewport()
    await expect_history_bottom(page)
    await expect(page.locator(".agentplane-user-bubble .agentplane-verbatim")).to_have_text(command.submit_input.text)
    await expect_input_confirmed(page)
    await expect(page.get_by_role("button", name="Retry", exact=True)).to_have_count(0)
    await expect(page.get_by_role("img", name="Streaming", exact=True)).to_have_count(0)
    await expect(page.get_by_role("img", name="Incomplete", exact=True)).to_have_count(1)
    await expect(composer).to_be_enabled()
    await expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_disabled()
    await expect(page.locator('img[src="x"]')).to_have_count(0)
    await page.screenshot(path=undeclared_outputs_dir() / f"{request.node.name}-failed.png")

    await page.reload()
    await expect(error_text).to_be_in_viewport()
    await expect(page.get_by_text("Test partial paragraph 29", exact=True)).to_have_count(1)
    await expect(page.locator(".agentplane-user-bubble .agentplane-verbatim")).to_have_text(command.submit_input.text)
    await expect_input_confirmed(page)
    if raw:
        (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
        await expect_archived_events(thread_browser.event_logs, thread.id, source.entries)
        lifecycle = page.locator(f'[data-thread-anchor="{failed.cursor}"]')
        await click_evidence(lifecycle)
        raw_frames = lifecycle.locator(
            ".agentplane-disclosure-summary", has_text=f"Observation {failed.cursor} raw frames"
        )
        await raw_frames.click()
        frames_panel = raw_frames.locator("xpath=../..").locator(".agentplane-disclosure-panel")
        # CodeBlock mounts its editor only near the viewport, replacing its placeholder when it does.
        # The mobile disclosure can open below the visible region after reload, so scroll the panel
        # into view -- the placeholder is never the target, as it can be swapped out mid-action.
        await frames_panel.scroll_into_view_if_needed()
        frame = frames_panel.locator(".agentplane-code-block")
        await expect(frame).to_contain_text("unsafe diagnostic")
        assert json_format.Parse(await frame.inner_text(), event_log_pb2.EventEntry()) == native
        await click_evidence(lifecycle)

    await composer.fill("Test distinct input after the failed turn")
    await composer.press("Enter")
    async with asyncio.timeout(15):
        following = await source.commands.get()
    assert following.command_id != command.command_id
    assert following.submit_input.text == "Test distinct input after the failed turn"
    source.append(event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="test-following-turn")))
    source.append(
        event_pb2.Event(
            harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
                harness_message_id="test-following-input",
                origin_command_ids=[following.command_id],
                text=following.submit_input.text,
                turn_id="test-following-turn",
            )
        )
    )
    source.append(
        event_pb2.Event(
            item_started=event_pb2.ItemStarted(item_id="test-following-reply", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
        )
    )
    source.append(
        event_pb2.Event(
            item_completed=event_pb2.ItemCompleted(item_id="test-following-reply", text="Test later successful reply")
        )
    )
    source.append(
        event_pb2.Event(
            turn_completed=event_pb2.TurnCompleted(
                turn_id="test-following-turn", status=event_pb2.TURN_STATUS_COMPLETED
            )
        )
    )
    await expect(page.get_by_text("Turn completed", exact=True)).to_have_count(1)
    await expect(page.get_by_text("Turn failed", exact=True)).to_have_count(1)
    await expect(page.get_by_text("Test later successful reply", exact=True)).to_have_count(1)
    await expect(error_text).to_have_count(1)
    await expect(page.locator(".agentplane-user-bubble .agentplane-verbatim")).to_have_text(
        [command.submit_input.text, following.submit_input.text]
    )
    await expect_input_confirmed(page)
    assert source.commands.empty()
    (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
    archived = await thread_browser.event_logs.events(thread.id, limit=100)
    assert archived == source.entries
    assert [entry.event.command_admitted.command for entry in archived if entry.event.HasField("command_admitted")] == [
        command,
        following,
    ]


@pytest.mark.parametrize("outcome", ["failed", "noop"])
async def test_settled_command_reason_survives_leaving_the_tail_and_reload(
    thread_browser: ThreadBrowser, outcome: str
) -> None:
    page, source = thread_browser.page, thread_browser.source
    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    submitted = "Test input whose outcome must remain visible"
    composer = message_composer(page)
    await composer.fill(submitted)
    await composer.press("Enter")
    async with asyncio.timeout(15):
        command = await source.commands.get()
    reason = f"Test command {outcome} after admission"
    if outcome == "failed":
        source.append(
            event_pb2.Event(command_failed=event_pb2.CommandFailed(command_id=command.command_id, reason=reason))
        )
    else:
        source.append(event_pb2.Event(command_noop=event_pb2.CommandNoop(command_id=command.command_id, reason=reason)))
    for index in range(40):
        item_id = f"after-command-{index}"
        source.append(
            event_pb2.Event(
                item_started=event_pb2.ItemStarted(item_id=item_id, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
            )
        )
        source.append(
            event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id=item_id, text=f"After command {index}"))
        )
    await expect_projected_cursor(page, source.entries[-1].cursor)
    await expect(page.get_by_text(reason, exact=False)).to_have_count(1)
    await expect(page.get_by_text(submitted, exact=True)).to_have_count(1)
    await expect(page.locator(f'.agentplane-user-bubble[data-message-phase="{outcome}"]')).to_have_count(1)
    await page.reload()
    await expect(page.get_by_text(reason, exact=False)).to_have_count(1)
    await expect(page.get_by_text(submitted, exact=True)).to_have_count(1)
    await expect(page.locator(f'.agentplane-user-bubble[data-message-phase="{outcome}"]')).to_have_count(1)
    await page.screenshot(path=undeclared_outputs_dir() / f"command-{outcome}-retained.png")
    await page.get_by_role("button", name="Dismiss", exact=True).click()
    await expect(page.get_by_text(reason, exact=False)).to_have_count(0)
    await expect(page.get_by_text(submitted, exact=True)).to_have_count(0)
    await page.reload()
    await expect_projected_cursor(page, source.entries[-1].cursor)
    await expect(page.get_by_text(reason, exact=False)).to_have_count(0)
    await expect(page.get_by_text(submitted, exact=True)).to_have_count(0)
    assert source.commands.empty()


async def test_browser_sends_a_command_and_transitions_its_message_to_confirmed_input(
    thread_browser: ThreadBrowser,
) -> None:
    page, source = thread_browser.page, thread_browser.source
    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    composer = message_composer(page)
    await composer.fill("Test input from the real browser")
    await composer.press("Enter")
    async with asyncio.timeout(15):
        command = await source.commands.get()
    assert command.command_id
    assert command.HasField("submit_input")
    assert command.submit_input.text == "Test input from the real browser"
    await expect_pending_message_bubble(page, command.submit_input.text)
    source.append(
        event_pb2.Event(
            harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
                harness_message_id="test-confirmed-message",
                origin_command_ids=[command.command_id],
                text=command.submit_input.text,
                turn_id="test-browser-turn",
            )
        )
    )
    await expect(page.locator(".agentplane-user-bubble .agentplane-verbatim")).to_have_text(command.submit_input.text)
    await expect(page.locator('.agentplane-user-bubble[data-message-phase="pending"]')).to_have_count(0)
    await expect(composer).to_have_value("")


async def test_reload_redelivers_an_unsaved_command_with_its_original_identity(thread_browser: ThreadBrowser) -> None:
    page, source = thread_browser.page, thread_browser.source
    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    intercepted: asyncio.Queue[Request] = asyncio.Queue()

    async def lose_request(route: Route) -> None:
        intercepted.put_nowait(route.request)
        await route.abort()

    await page.route("**/threads/*/commands", lose_request, times=1)
    composer = message_composer(page)
    await composer.fill("Test input retained across an unsent request")
    await composer.press("Enter")
    async with asyncio.timeout(15):
        original = await intercepted.get()
    assert original.post_data is not None
    command = json_format.Parse(original.post_data, command_pb2.Command())
    assert command.command_id
    assert command.submit_input.text == "Test input retained across an unsent request"
    await expect(page.get_by_role("button", name="Retry", exact=True)).to_be_enabled()
    (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
    assert await thread_browser.event_logs.events(thread.id, limit=100) == source.entries[:4]

    async with page.expect_request(original.url) as redelivered:
        await page.reload()
    redelivery = await redelivered.value
    assert redelivery.post_data is not None
    assert json_format.Parse(redelivery.post_data, command_pb2.Command()) == command
    async with asyncio.timeout(15):
        assert await source.commands.get() == command
    await expect_pending_message_bubble(page, command.submit_input.text)
    await expect(page.get_by_text("Saved locally · awaiting admission", exact=True)).to_have_count(0)
    await expect(page.get_by_role("button", name="Retry", exact=True)).to_have_count(0)
    # The command receipt can arrive before its admission event is replicated into the app.
    await expect_projected_cursor(page, source.entries[-1].cursor)
    admissions = [
        entry.event.command_admitted.command
        for entry in await thread_browser.event_logs.events(thread.id, limit=100)
        if entry.event.HasField("command_admitted")
    ]
    assert admissions == [command]


async def expect_input_confirmed(page: Page) -> None:
    """Every input bubble is the confirmed message, none still local, pending, failed or no-op."""
    await expect(page.locator('.agentplane-user-bubble:not([data-message-phase="confirmed"])')).to_have_count(0)


async def expect_pending_message_bubble(page: Page, text: str) -> None:
    """A submitInput command the server has admitted and is still working on renders inline as the
    same bubble a confirmed message gets, marked pending (italic) until the harness effects it."""
    bubble = page.locator(".agentplane-user-bubble")
    await expect(bubble.locator(".agentplane-verbatim")).to_have_text(text)
    await expect(bubble).to_have_css("font-style", "italic")


async def test_streamed_admission_survives_a_lost_http_reply_and_reload(thread_browser: ThreadBrowser) -> None:
    page, source = thread_browser.page, thread_browser.source
    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    replies: asyncio.Queue[APIResponse] = asyncio.Queue()
    drop_reply = asyncio.Event()
    reply_started = asyncio.Event()
    reply_finished = asyncio.Event()

    async def hold_reply(route: Route) -> None:
        # This is the runner's durable receipt. Only delivery to this browser is withheld;
        # independent app archival and Electric synchronization continue.
        reply_started.set()
        try:
            replies.put_nowait(await route.fetch())
            await drop_reply.wait()
            await route.abort()
        finally:
            reply_finished.set()

    await page.route("**/threads/*/commands", hold_reply, times=1)
    try:
        composer = message_composer(page)
        await composer.fill("Test input saved without its HTTP reply")
        await composer.press("Enter")
        async with asyncio.timeout(15):
            response = await replies.get()
            command = await source.commands.get()
        assert response.status == 200
        admission = json_format.Parse(await response.text(), event_log_pb2.EventEntry())
        assert admission.event.command_admitted.command == command
        (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
        await expect_pending_message_bubble(page, command.submit_input.text)
        archived = await expect_archived_events(thread_browser.event_logs, thread.id, source.entries)
        assert admission in archived

        async with page.expect_event("requestfailed", predicate=lambda request: request.url == response.url):
            drop_reply.set()
        await expect_pending_message_bubble(page, command.submit_input.text)
        await expect(page.get_by_text("Saved locally · awaiting admission", exact=True)).to_have_count(0)
        await page.reload()
        await expect_pending_message_bubble(page, command.submit_input.text)
        await expect(page.get_by_role("button", name="Retry", exact=True)).to_have_count(0)
    finally:
        drop_reply.set()
        if reply_started.is_set():
            async with asyncio.timeout(15):
                await reply_finished.wait()
        await page.unroute_all(behavior="wait")


async def test_lost_runner_receipt_reconciles_from_thread_without_retry(thread_browser: ThreadBrowser) -> None:
    page, source = thread_browser.page, thread_browser.source
    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    receipts: asyncio.Queue[APIResponse] = asyncio.Queue()

    async def lose_receipt(route: Route) -> None:
        receipt = await route.fetch()
        receipts.put_nowait(receipt)
        await route.fulfill(status=504, content_type="application/json", body='{"detail":"outcome uncertain"}')

    await page.route("**/threads/*/commands", lose_receipt, times=1)
    composer = message_composer(page)
    await composer.fill("Test late admission from feed")
    await composer.press("Enter")
    async with asyncio.timeout(15):
        receipt = await receipts.get()
        original = await source.commands.get()
    assert receipt.status == 200
    admitted = json_format.Parse(await receipt.text(), event_log_pb2.EventEntry())
    assert admitted.event.command_admitted.command == original
    (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
    assert admitted in await expect_archived_events(thread_browser.event_logs, thread.id, source.entries)
    await expect_pending_message_bubble(page, original.submit_input.text)
    await expect(page.get_by_text("Admission unconfirmed · checking Thread history", exact=True)).to_have_count(0)
    await expect(page.get_by_role("button", name="Retry", exact=True)).to_have_count(0)
    await page.reload()
    await expect_pending_message_bubble(page, original.submit_input.text)
    assert source.commands.empty()


async def test_unconfirmed_command_retries_with_same_identity_then_reconciles(thread_browser: ThreadBrowser) -> None:
    page, source = thread_browser.page, thread_browser.source
    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    requests: asyncio.Queue[command_pb2.Command] = asyncio.Queue()

    async def lose_receipt(route: Route) -> None:
        assert route.request.post_data is not None
        requests.put_nowait(json_format.Parse(route.request.post_data, command_pb2.Command()))
        await route.fulfill(status=504, content_type="application/json", body='{"detail":"outcome uncertain"}')

    await page.route("**/threads/*/commands", lose_receipt, times=1)
    composer = message_composer(page)
    await composer.fill("Test uncertain admission")
    await composer.press("Enter")
    async with asyncio.timeout(15):
        original = await requests.get()
    bubble = page.locator(f'[data-command-id="{original.command_id}"]')
    await expect(bubble.get_by_text("Admission unconfirmed · checking Thread history", exact=True)).to_be_visible()
    await expect(bubble.get_by_text("Input failed", exact=False)).to_have_count(0)
    async with page.expect_request("**/threads/*/commands") as retried:
        await bubble.get_by_role("button", name="Retry", exact=True).click()
    retry = await retried.value
    assert retry.post_data is not None
    assert json_format.Parse(retry.post_data, command_pb2.Command()) == original
    async with asyncio.timeout(15):
        assert await source.commands.get() == original
    await expect_pending_message_bubble(page, original.submit_input.text)
    (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
    archived = await expect_archived_events(thread_browser.event_logs, thread.id, source.entries)
    assert any(
        entry.event.HasField("command_admitted") and entry.event.command_admitted.command == original
        for entry in archived
    )
    await expect(bubble.get_by_text("Admission unconfirmed · checking Thread history", exact=True)).to_have_count(0)


async def click_evidence(scope: Locator) -> None:
    """Click the Evidence toggle under `scope` the way a reader reaches it: it shows while its item is hovered."""
    toggle = scope.locator(".agentplane-evidence-toggle")
    await toggle.locator("xpath=ancestor::*[contains(@class, 'agentplane-evidence-owner')][1]").hover()
    await toggle.click()


async def expand_item_evidence(page: Page) -> None:
    await click_evidence(page.locator('[data-thread-anchor="3"]'))


async def open_debug_history(page: Page) -> None:
    """Debug history lives in the composer's overflow menu, not a standalone button."""
    await page.get_by_role("button", name="More", exact=True).click()
    await page.get_by_role("menuitem", name="Debug history", exact=True).click()


@pytest.mark.parametrize("replay_after", [4])
async def test_unobserved_committed_admission_reconciles_once_after_reload(thread_browser: ThreadBrowser) -> None:
    page, source, app = thread_browser.page, thread_browser.source, thread_browser.app
    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    replies: asyncio.Queue[APIResponse] = asyncio.Queue()
    drop_reply = asyncio.Event()

    async def lose_committed_reply(route: Route) -> None:
        replies.put_nowait(await route.fetch())
        await drop_reply.wait()
        await route.abort()

    await page.route("**/threads/*/commands", lose_committed_reply, times=1)
    try:
        composer = message_composer(page)
        await composer.fill("Test input whose admission neither browser channel observed")
        await composer.press("Enter")
        async with asyncio.timeout(15):
            response = await replies.get()
            command = await source.commands.get()
            assert (await app.replay_held()).cursor >= 5
        assert response.status == 200
        admission = json_format.Parse(await response.text(), event_log_pb2.EventEntry())
        assert admission.event.command_admitted.command == command
        assert admission == source.entries[4]
        (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
        assert await thread_browser.event_logs.events(thread.id, limit=100) == source.entries

        async with page.expect_event("requestfailed", predicate=lambda request: request.url == response.url):
            drop_reply.set()
        pending = page.get_by_role("region", name="Input messages")
        await expect(pending.locator("[data-command-id]")).to_have_attribute("data-command-id", command.command_id)
        await expect(pending.get_by_text("Admission unconfirmed · checking Thread history", exact=True)).to_be_visible()
        await expect(page.locator('.agentplane-user-bubble[data-message-phase="local"]')).to_have_count(1)

        # Reload abandons the held Electric response. The new document delivers the same local
        # Command again, and the app answers from its archive while replay is still held.
        async with page.expect_response(response.url) as redelivered:
            await page.reload()
        redelivery = await redelivered.value
        assert redelivery.status == 200
        assert json_format.Parse(await redelivery.text(), event_log_pb2.EventEntry()) == admission
        async with asyncio.timeout(15):
            assert (await app.replay_held()).cursor >= 5
        await expect(pending.locator("[data-command-id]")).to_have_attribute("data-command-id", command.command_id)
        await expect(pending.get_by_text(command.submit_input.text, exact=True)).to_be_visible()
        await expect(pending.get_by_text("Saved · awaiting effect", exact=True)).to_be_visible()
        await expect(page.get_by_text("Catching up thread…", exact=True)).to_be_visible()
        assert source.commands.empty(), "reload must not manufacture a second command"

        app.release_replay()
        await expect_pending_message_bubble(page, command.submit_input.text)
        await expect(pending).to_have_count(0)
        await expect(pending.get_by_text("Saved locally · awaiting admission", exact=True)).to_have_count(0)
        await expect(page.get_by_role("button", name="Retry", exact=True)).to_have_count(0)
        source.append(
            event_pb2.Event(
                harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
                    harness_message_id="test-input-recovered-from-unobserved-admission",
                    origin_command_ids=[command.command_id],
                    text=command.submit_input.text,
                    turn_id="test-browser-turn",
                )
            )
        )
        await expect(page.locator(".agentplane-user-bubble .agentplane-verbatim")).to_have_text(
            command.submit_input.text
        )
        await expect(pending).to_have_count(0)
        assert source.commands.empty(), "the runner must receive the Command once"
        archived = await expect_archived_events(thread_browser.event_logs, thread.id, source.entries)
        assert [entry for entry in archived if entry.event.HasField("command_admitted")] == [admission]
    finally:
        drop_reply.set()
        await page.unroute_all(behavior="wait")


@pytest.mark.parametrize("replay_after", [4])
async def test_http_admission_ahead_of_replay_does_not_skip_earlier_events(thread_browser: ThreadBrowser) -> None:
    page, source, app = thread_browser.page, thread_browser.source, thread_browser.app
    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    for text in (" and preceding delta A", " and preceding delta B"):
        source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="test-browser-item", text=text)))
    async with asyncio.timeout(15):
        assert (await app.replay_held()).cursor >= 5

    composer = message_composer(page)
    await composer.fill("Test input admitted ahead of the browser prefix")
    async with page.expect_response(lambda response: response.url.endswith("/commands")) as replied:
        await composer.press("Enter")
    response = await replied.value
    async with asyncio.timeout(15):
        command = await source.commands.get()
    assert response.status == 200
    admission = json_format.Parse(await response.text(), event_log_pb2.EventEntry())
    assert admission.event.command_admitted.command == command
    assert admission == source.entries[6]
    (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
    await expect_archived_events(thread_browser.event_logs, thread.id, source.entries)

    pending = page.get_by_role("region", name="Input messages")
    await expect(pending.get_by_text("Saved · awaiting effect", exact=True)).to_be_visible()
    await expect(pending.locator("[data-command-id]")).to_have_attribute("data-command-id", command.command_id)
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()

    app.release_replay()
    await expect(
        page.get_by_text("Test retained prefix and preceding delta A and preceding delta B", exact=True)
    ).to_have_count(1)
    await expect_pending_message_bubble(page, command.submit_input.text)
    source.append(
        event_pb2.Event(
            harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
                harness_message_id="test-input-after-replay-catch-up",
                origin_command_ids=[command.command_id],
                text=command.submit_input.text,
                turn_id="test-browser-turn",
            )
        )
    )
    await expect(page.locator(".agentplane-user-bubble .agentplane-verbatim")).to_have_text(command.submit_input.text)
    await expect(pending).to_have_count(0)
    assert source.commands.empty()
    await expect_archived_events(thread_browser.event_logs, thread.id, source.entries)


@pytest.mark.parametrize("replay_after", [4])
async def test_electric_reconnects_unconfirmed_command_without_reloading(thread_browser: ThreadBrowser) -> None:
    page, source, app = thread_browser.page, thread_browser.source, thread_browser.app
    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    document = await page.evaluate_handle("document")
    submissions: list[Request] = []

    def record_submission(request: Request) -> None:
        if request.url.endswith("/commands"):
            submissions.append(request)

    page.on("request", record_submission)
    replies: asyncio.Queue[APIResponse] = asyncio.Queue()
    drop_reply = asyncio.Event()

    async def lose_committed_reply(route: Route) -> None:
        replies.put_nowait(await route.fetch())
        await drop_reply.wait()
        await route.abort()

    await page.route("**/threads/*/commands", lose_committed_reply, times=1)
    try:
        composer = message_composer(page)
        await composer.fill("Test input pending across Electric reconnect")
        await composer.press("Enter")
        async with asyncio.timeout(15):
            response = await replies.get()
            command = await source.commands.get()
            assert (await app.replay_held()).cursor >= 5
        assert response.status == 200
        admission = json_format.Parse(await response.text(), event_log_pb2.EventEntry())
        assert admission == source.entries[4]
        assert admission.event.command_admitted.command == command
        async with page.expect_event("requestfailed", predicate=lambda request: request.url == response.url):
            drop_reply.set()
        pending = page.get_by_role("region", name="Input messages")
        await expect(pending.get_by_text("Admission unconfirmed · checking Thread history", exact=True)).to_be_visible()

        # Interrupt real shape delivery. The published Electric client must retry its own
        # handle/offset, without a document reload or an Agentplane event replay reducer.
        async with page.expect_request(lambda request: "/sync/entities?" in request.url) as reconnecting:
            app.disconnect_replay()
            for text in (" and disconnected delta A", " and disconnected delta B"):
                source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="test-browser-item", text=text)))
        reconnect = await reconnecting.value
        continuation = parse_qs(urlsplit(reconnect.url).query)
        assert continuation["handle"]
        assert continuation["offset"] != ["-1"]
        async with asyncio.timeout(15):
            assert (await app.replay_held()).cursor >= 5
        assert await document.evaluate("original => original === document")
        await expect(pending.locator("[data-command-id]")).to_have_attribute("data-command-id", command.command_id)
        await expect(pending.get_by_text("Admission unconfirmed · checking Thread history", exact=True)).to_be_visible()
        await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()

        app.release_replay()
        await expect(
            page.get_by_text("Test retained prefix and disconnected delta A and disconnected delta B", exact=True)
        ).to_have_count(1)
        await expect_pending_message_bubble(page, command.submit_input.text)
        source.append(
            event_pb2.Event(
                harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
                    harness_message_id="test-input-after-electric-reconnect",
                    origin_command_ids=[command.command_id],
                    text=command.submit_input.text,
                    turn_id="test-browser-turn",
                )
            )
        )
        await expect(page.locator(".agentplane-user-bubble .agentplane-verbatim")).to_have_text(
            command.submit_input.text
        )
        await expect(pending).to_have_count(0)
        assert await document.evaluate("original => original === document")
        assert len(submissions) == 1
        assert source.commands.empty()
        (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
        await expect_archived_events(thread_browser.event_logs, thread.id, source.entries)
    finally:
        drop_reply.set()
        await page.unroute_all(behavior="wait")
        await document.dispose()


async def test_terminal_shape_error_keeps_rows_until_a_refresh_replaces_the_window(
    thread_browser: ThreadBrowser,
) -> None:
    page, source = thread_browser.page, thread_browser.source
    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    composer = message_composer(page)
    await composer.fill("Command retained across terminal shape error")
    await composer.press("Enter")
    async with asyncio.timeout(15):
        command = await source.commands.get()
    pending = page.get_by_role("region", name="Input messages")
    await expect_pending_message_bubble(page, command.submit_input.text)

    async def terminal_shape_error(route: Route) -> None:
        await route.fulfill(status=400, content_type="text/plain", body="shape rejected")

    await page.route("**/sync/entities?*", terminal_shape_error, times=1)
    try:
        # Dropping the connection ends the live SSE response; the client's reconnect is the one the
        # route answers.
        async with page.expect_response(lambda response: "/sync/entities?" in response.url and response.status == 400):
            await thread_browser.ingress.drop_connections()
        stopped = page.get_by_role("alert").filter(has_text="Thread synchronization stopped:")
        await expect(stopped).to_be_visible()
        await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
        await expect_pending_message_bubble(page, command.submit_input.text)

        async with page.expect_response(lambda response: "/sync/scope" in response.url and response.status == 200):
            await stopped.get_by_role("button", name="Refresh thread", exact=True).click()
        await expect(page.get_by_text("Thread synchronization stopped:", exact=False)).to_have_count(0)
        await expect_pending_message_bubble(page, command.submit_input.text)
        source.append(
            event_pb2.Event(
                harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
                    harness_message_id="test-command-after-terminal-shape-refresh",
                    origin_command_ids=[command.command_id],
                    text=command.submit_input.text,
                    turn_id="test-browser-turn",
                )
            )
        )
        await expect(page.locator(".agentplane-user-bubble .agentplane-verbatim")).to_have_text(
            command.submit_input.text
        )
        await expect(pending).to_have_count(0)
    finally:
        await page.unroute("**/sync/entities?*", terminal_shape_error)


async def test_thread_says_it_is_reconnecting_while_electric_retries_a_dropped_connection(
    thread_browser: ThreadBrowser,
) -> None:
    page, source = thread_browser.page, thread_browser.source
    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    # Electric's connection is separate from the shared Threads snapshot. Its indicator
    # reports the outage, while both dots use the same Threads verdict (not an Electric-specific label).
    indicator = page.get_by_role("img", name=re.compile(r"\bThread: reconnecting since "))
    await expect(indicator).to_have_count(0)

    await page.context.set_offline(True)
    await thread_browser.ingress.drop_connections()
    await expect(indicator).to_be_visible(timeout=15_000)
    await expect(page.get_by_role("img", name="No live harness confirmed", exact=True)).to_have_count(2)
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()

    await page.context.set_offline(False)
    await expect(indicator).to_have_count(0, timeout=35_000)
    source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="test-browser-item", text=" and reconnected")))
    await expect(page.get_by_text("Test retained prefix and reconnected", exact=True)).to_be_visible()


async def test_ahead_snapshot_is_not_a_thread_or_effective_model(thread_browser: ThreadBrowser) -> None:
    page = thread_browser.page
    await expect(page.get_by_role("status")).to_have_text("Loading thread…")
    await expect(page.locator('[aria-label="Model"]:enabled')).to_have_count(0)
    await expect(page.locator("textarea:enabled")).to_have_count(0)
    await expect(page.locator('[aria-label="Interrupt"]:enabled')).to_have_count(0)
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_have_count(0)
    (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
    assert await thread_browser.event_logs.last_cursor(thread.id) == 0

    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    await expect(page.get_by_role("status")).to_have_count(0)
    await expect(page.get_by_role("combobox", name="Model", exact=True)).to_have_value("Test Model Before")
    await expect(page.get_by_role("combobox", name="Model", exact=True)).to_be_enabled()
    await expect(message_composer(page)).to_be_enabled()
    await expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_enabled()
    assert await thread_browser.event_logs.events(thread.id, limit=100) == thread_browser.source.entries


async def test_rejected_source_suffix_stops_browser_without_replacing_verified_history(
    thread_browser: ThreadBrowser,
) -> None:
    page, source = thread_browser.page, thread_browser.source
    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    await expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_enabled()

    # Corrupt the controlled upstream entry before the next event-loop yield can publish it.
    rejected = source.append(
        event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="test-browser-item", text=" INVALID SUFFIX"))
    )
    rejected.cursor = 6
    rejected.origin.sequence = 6

    await expect(page.get_by_role("alert")).to_contain_text("expected runner cursor 5, received 6")
    await expect(page.get_by_role("alert")).to_contain_text("Showing verified history through event 4")
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_have_count(1)
    await expect(page.get_by_text("INVALID SUFFIX", exact=False)).to_have_count(0)
    await expect(page.get_by_role("combobox", name="Model", exact=True)).to_be_disabled()
    await expect(message_composer(page)).to_be_disabled()
    await expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_disabled()
    (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
    assert await thread_browser.event_logs.events(thread.id, limit=100) == source.entries[:4]


async def test_unknown_projection_failure_keeps_verified_history_and_stops_browser(
    thread_browser: ThreadBrowser,
) -> None:
    page, source, store = thread_browser.page, thread_browser.source, thread_browser.store
    await thread_browser.start_replay()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    composer = message_composer(page)
    draft = "Retained draft while projection failure is reported"
    await composer.fill(draft)

    # This models only the persisted result of a batch-wide projection failure. The real
    # PostgreSQL/Electric/Chromium path must render it; no malformed native event is simulated.
    (thread,) = await store.list_threads(sandbox=SANDBOX)
    async with store._sessions() as session, session.begin():
        checkpoint = await session.get(ThreadCheckpoint, thread.id)
        assert checkpoint is not None
        view = await session.get(ThreadEntity, (thread.id, checkpoint.projection_epoch, "view_state", "current"))
        assert view is not None
        feed = await session.get(FeedState, thread.id)
        assert feed is not None
        state = ThreadViewState.model_validate(view.state)
        view.state = state.model_copy(
            update={
                "operational": ThreadOperationalState(
                    status="failed",
                    last_verified_cursor=str(checkpoint.through_cursor),
                    feed_error=ThreadFeedErrorState(cursor=None, message="batch-wide projection invariant failed"),
                )
            }
        ).model_dump(mode="json")
        feed.end = {"message": "batch-wide projection invariant failed"}

    failure = page.get_by_role("alert")
    await expect(failure).to_contain_text("Projection failed: batch-wide projection invariant failed.")
    await expect(failure).to_contain_text("Showing verified history through event 4.")
    await expect(failure).not_to_contain_text("Rejected event")
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_have_count(1)
    await expect(composer).to_have_value(draft)
    await expect(page.get_by_role("combobox", name="Model", exact=True)).to_be_disabled()
    await expect(composer).to_be_disabled()
    await expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_disabled()
    await page.screenshot(path=undeclared_outputs_dir() / "unknown-projection-failure-retained.png")

    await page.reload()
    await expect(failure).to_contain_text("Projection failed: batch-wide projection invariant failed.")
    await expect(failure).to_contain_text("Showing verified history through event 4.")
    await expect(failure).not_to_contain_text("Rejected event")
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_have_count(1)
    await expect(page.get_by_role("combobox", name="Model", exact=True)).to_be_disabled()
    await expect(composer).to_be_disabled()
    await expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_disabled()
    assert await thread_browser.event_logs.events(thread.id, limit=100) == source.entries


if __name__ == "__main__":
    pytest_bazel.main()
