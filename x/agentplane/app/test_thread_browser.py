"""The built SPA consumes an actual PostgreSQL-backed HTTP and Connect stream in a real Chromium page.

Only the upstream runner protocol source and Kubernetes/auth boundaries are controlled. Browser
fetch, streaming, rendering, and page reload are not replaced by the visual harness's mocks.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta

import pytest
import pytest_bazel
from google.protobuf import json_format
from playwright.async_api import APIResponse, Page, Request, Route, async_playwright, expect

from util.bazel.runfiles import get_required_path
from util.testing.frontend_visual import CONTAINER_BASE_BROWSER_ARGS, chromium_executable
from util.testing.undeclared_outputs import undeclared_outputs_dir
from x.agentplane.app import thread_events_pb2
from x.agentplane.app.testing.replication_process import AppProcess, app_process
from x.agentplane.app.testing.replication_source import SANDBOX, SESSION, Opened, ReplicationSource
from x.agentplane.app.thread_events import FOLLOW_EVENTS_PATH
from x.agentplane.app.trajectory import TrajectoryStore
from x.agentplane.protocol import command_pb2, event_log_pb2, event_pb2

# gazelle:include_dep @pypi//protobuf


def _follow_from(body: bytes) -> int:
    """The cursor a reopened stream asks to resume after, out of its Connect request envelope."""
    request = thread_events_pb2.FollowEventsRequest()
    request.ParseFromString(body[5 : 5 + int.from_bytes(body[1:5], "big")])
    return request.after_cursor


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
    app: AppProcess


@pytest.fixture
def replay_after() -> int | None:
    return None


@pytest.fixture
def thread_source() -> ReplicationSource:
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
    return source


@pytest.fixture
async def thread_browser(
    page: Page, db_url: str, store: TrajectoryStore, thread_source: ReplicationSource, replay_after: int | None
) -> AsyncIterator[ThreadBrowser]:
    source = thread_source
    thread_id = await store.thread(SANDBOX, SESSION, source.attached.spec)
    directory = get_required_path("_main/x/agentplane/app/frontend/dist/index.html").parent
    async with (
        source.serve() as target,
        app_process(db_url, target, frontend_directory=directory, replay_after=replay_after) as app,
    ):
        async with asyncio.timeout(30):
            opened = await source.opened.get()
            await page.goto(f"{app.url}/#/threads/{thread_id}")
        yield ThreadBrowser(page, source, store, opened, app)


async def test_archived_thread_page_survives_deleted_sandbox_and_reload(
    page: Page, db_url: str, store: TrajectoryStore, thread_source: ReplicationSource
) -> None:
    thread_id = await store.thread(SANDBOX, SESSION, thread_source.attached.spec)
    lease = await store.acquire_ingestion(SANDBOX, timedelta(minutes=1))
    assert lease is not None
    await store.set_attached(thread_id, thread_source.attached, lease=lease)
    await store.record(thread_id, thread_source.entries, lease=lease)
    await store.rename(thread_id, "Test archived conversation")
    await store.release_ingestion(lease)
    directory = get_required_path("_main/x/agentplane/app/frontend/dist/index.html").parent
    async with app_process(db_url, "127.0.0.1:1", frontend_directory=directory, sandbox_state=None) as app:
        url = f"{app.url}/#/threads/{thread_id}"
        await page.goto(url)
        await expect(page.get_by_role("textbox", name="Thread name", exact=True)).to_have_value(
            "Test archived conversation"
        )
        await expect(page.get_by_text("Test retained prefix", exact=True)).to_have_count(1)
        await expect(
            page.get_by_text("Sandbox no longer exists. Showing archived Thread history.", exact=True)
        ).to_be_visible()
        await expect(page.get_by_placeholder("Enter sends, Ctrl+Enter for a new line")).to_be_disabled()
        await expect(page.get_by_role("combobox", name="Model", exact=True)).to_be_disabled()
        await expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_disabled()
        await expect(page.get_by_role("img", name="Streaming", exact=True)).to_have_count(0)
        await expect(page.get_by_role("img", name="Incomplete in retained history", exact=True)).to_have_count(1)
        await page.reload()
        await expect(page).to_have_url(url)
        await expect(page.get_by_text("Test retained prefix", exact=True)).to_have_count(1)
        await expect(
            page.get_by_text("Sandbox no longer exists. Showing archived Thread history.", exact=True)
        ).to_be_visible()
        assert await store.events(thread_id, limit=100) == thread_source.entries


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

    # Reload replaces the JS document, including component memory and its open stream. Durable
    # replay must rebuild the same text once, not append the prefix to the live card a second time.
    await page.reload()
    await expect(page.get_by_text(complete_text, exact=True)).to_have_count(1)
    await expect(page.get_by_role("img", name="Streaming", exact=True)).to_have_count(0)
    await expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_disabled()
    (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
    assert await thread_browser.store.events(thread.id, limit=100) == source.entries


async def test_sidebar_receives_rename_and_archive_from_another_app_replica(thread_browser: ThreadBrowser) -> None:
    page = thread_browser.page
    thread_browser.opened.replay.set()
    sidebar = page.get_by_role("navigation", name="Threads", exact=True)
    await expect(sidebar.get_by_text(SESSION, exact=True)).to_be_visible()
    (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
    await thread_browser.store.rename(thread.id, "Test rename from another replica")
    await expect(sidebar.get_by_text("Test rename from another replica", exact=True)).to_be_visible()
    await expect(sidebar.get_by_text(SESSION, exact=True)).to_have_count(0)
    await thread_browser.store.archive(thread.id)
    await expect(sidebar.get_by_text("Test rename from another replica", exact=True)).to_have_count(0)
    archived_switch = page.get_by_role("switch", name="Show archived threads", exact=True)
    await archived_switch.press("Space")
    await expect(archived_switch).to_be_checked()
    await expect(sidebar.get_by_text("Test rename from another replica", exact=True)).to_be_visible()
    await expect(page).to_have_url(f"{thread_browser.app.url}/#/threads/{thread.id}")
    await page.screenshot(path=undeclared_outputs_dir() / "sidebar-replica-updates.png")


@pytest.mark.parametrize("raw", [False, True], ids=["normal", "raw"])
@pytest.mark.parametrize("phone", [False, True], ids=["desktop", "phone"])
async def test_conversation_follows_bottom_until_reader_scrolls_up(
    thread_browser: ThreadBrowser, raw: bool, phone: bool, request: pytest.FixtureRequest
) -> None:
    page, source = thread_browser.page, thread_browser.source
    if phone:
        await page.set_viewport_size({"width": 412, "height": 915})
    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    if raw:
        await show_raw(page)
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
    await page.set_viewport_size({"width": 360 if phone else 800, "height": 650})
    await expect_history_bottom(page)
    await page.screenshot(path=undeclared_outputs_dir() / f"{request.node.name}-following.png")

    await history.hover()
    await page.mouse.wheel(0, -600)
    await page.wait_for_function(
        """() => {
            const area = document.querySelector('[aria-label="Thread history"]');
            return area.scrollHeight - area.clientHeight - area.scrollTop > 400;
        }"""
    )
    await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    reading_position = await history.evaluate("area => area.scrollTop")
    source.append(
        event_pb2.Event(
            text_delta=event_pb2.TextDelta(item_id="test-scroll-tail", text="\n\nTest output while reading")
        )
    )
    await expect(page.get_by_text("Test output while reading", exact=True)).to_have_count(1)
    source.append(
        event_pb2.Event(
            item_started=event_pb2.ItemStarted(item_id="test-scroll-next", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
        )
    )
    source.append(
        event_pb2.Event(
            item_completed=event_pb2.ItemCompleted(item_id="test-scroll-next", text="Test new message while reading")
        )
    )
    await expect(page.get_by_text("Test new message while reading", exact=True)).to_have_count(1)
    # Wait for the paint following layout/ResizeObserver, so a premature assertion cannot miss
    # an unwanted jump scheduled by that observer. No elapsed-time delay stands in for rendering.
    await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    assert await history.evaluate("area => area.scrollTop") == reading_position
    await page.set_viewport_size({"width": 360 if phone else 800, "height": 700})
    await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    assert await history.evaluate("area => area.scrollTop") == reading_position
    # A late expansion above the reader can advance scrollTop through browser anchoring.
    # Passing the old bottom that way must not be mistaken for returning to it.
    previous_bottom = await history.evaluate("area => area.scrollHeight - area.clientHeight")
    await page.get_by_text("Test retained prefix", exact=True).evaluate(
        """message => {
            const area = message.closest('[aria-label="Thread history"]');
            message.style.minHeight = `${area.scrollHeight}px`;
        }"""
    )
    await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    assert await history.evaluate("area => area.scrollTop") > previous_bottom
    assert await history.evaluate("area => area.scrollHeight - area.clientHeight - area.scrollTop") > 400
    await page.screenshot(path=undeclared_outputs_dir() / f"{request.node.name}-reading.png")

    # Scroll events are queued. Grow a rendered item in the same task as returning to the
    # bottom: when that event arrives, the old bottom is already behind the new content.
    # This reproduces a late layout expansion without relying on network/frame timing.
    await page.get_by_text("Test new message while reading", exact=True).evaluate(
        """message => {
            const area = message.closest('[aria-label="Thread history"]');
            area.scrollTo({ top: area.scrollHeight });
            message.style.minHeight = '240px';
        }"""
    )
    await expect_history_bottom(page)
    source.append(
        event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="test-scroll-tail", text="\n\nTest following again"))
    )
    await expect(page.get_by_text("Test following again", exact=True)).to_have_count(1)
    await expect_history_bottom(page)
    # Increasing the viewport height must keep following too, including browser scroll clamping.
    await page.set_viewport_size({"width": 412 if phone else 1280, "height": 900})
    await expect_history_bottom(page)
    await page.screenshot(path=undeclared_outputs_dir() / f"{request.node.name}-resumed.png")


async def expect_history_bottom(page: Page) -> None:
    await page.wait_for_function(
        """() => {
            const area = document.querySelector('[aria-label="Thread history"]');
            return area.scrollHeight - area.clientHeight - area.scrollTop <= 2;
        }"""
    )


@pytest.mark.parametrize("raw", [False, True], ids=["desktop-normal", "phone-raw"])
async def test_failed_turn_preserves_confirmed_input_and_allows_another_turn(
    thread_browser: ThreadBrowser, raw: bool, request: pytest.FixtureRequest
) -> None:
    page, source = thread_browser.page, thread_browser.source
    if raw:
        await page.set_viewport_size({"width": 412, "height": 915})
    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    if raw:
        await show_raw(page)
    composer = page.get_by_placeholder("Enter sends, Ctrl+Enter for a new line")
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
    error_text = page.get_by_text(diagnostic, exact=True)
    await expect(error_text).to_have_count(1)
    await expect(error_text).to_be_in_viewport()
    await expect_history_bottom(page)
    await expect(page.locator(".agentplane-user-bubble")).to_have_text(command.submit_input.text)
    await expect(page.get_by_role("region", name="Pending commands")).to_have_count(0)
    await expect(page.get_by_role("button", name="Retry", exact=True)).to_have_count(0)
    await expect(page.get_by_role("img", name="Streaming", exact=True)).to_have_count(0)
    await expect(page.get_by_role("img", name="Incomplete in retained history", exact=True)).to_have_count(1)
    await expect(composer).to_be_enabled()
    await expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_disabled()
    await expect(page.locator('img[src="x"]')).to_have_count(0)
    await page.screenshot(path=undeclared_outputs_dir() / f"{request.node.name}-failed.png")

    await page.reload()
    await expect(error_text).to_be_in_viewport()
    await expect(page.get_by_text("Test partial paragraph 29", exact=True)).to_have_count(1)
    await expect(page.locator(".agentplane-user-bubble")).to_have_text(command.submit_input.text)
    await expect(page.get_by_role("region", name="Pending commands")).to_have_count(0)
    if raw:
        await expect_raw_prefix(page, failed.cursor)
        for entry in (native, failed):
            frame = page.locator(f'[data-event-cursor="{entry.cursor}"]')
            await frame.locator("summary").click()
            assert (
                json_format.Parse(await frame.locator("[data-event-envelope]").inner_text(), event_log_pb2.EventEntry())
                == entry
            )
            await frame.locator("summary").click()

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
    await expect(page.get_by_text("Turn test-following-turn: COMPLETED", exact=True)).to_have_count(1)
    await expect(page.get_by_text("Test later successful reply", exact=True)).to_have_count(1)
    await expect(error_text).to_have_count(1)
    await expect(page.locator(".agentplane-user-bubble")).to_have_text(
        [command.submit_input.text, following.submit_input.text]
    )
    await expect(page.get_by_role("region", name="Command outcomes")).to_have_count(0)
    assert source.commands.empty()
    (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
    archived = await thread_browser.store.events(thread.id, limit=100)
    assert archived == source.entries
    assert [entry.event.command_admitted.command for entry in archived if entry.event.HasField("command_admitted")] == [
        command,
        following,
    ]


async def test_browser_sends_a_command_and_renders_only_the_confirmed_input(thread_browser: ThreadBrowser) -> None:
    page, source = thread_browser.page, thread_browser.source
    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    composer = page.get_by_placeholder("Enter sends, Ctrl+Enter for a new line")
    await composer.fill("Test input from the real browser")
    await composer.press("Enter")
    async with asyncio.timeout(15):
        command = await source.commands.get()
    assert command.command_id
    assert command.HasField("submit_input")
    assert command.submit_input.text == "Test input from the real browser"
    await expect(page.locator(".agentplane-user-bubble")).to_have_count(0)
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
    await expect(page.locator(".agentplane-user-bubble")).to_have_text(command.submit_input.text)
    await expect(composer).to_have_value("")


async def test_reload_retries_an_unsaved_command_with_its_original_identity(thread_browser: ThreadBrowser) -> None:
    page, source = thread_browser.page, thread_browser.source
    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    intercepted: asyncio.Queue[Request] = asyncio.Queue()

    async def lose_request(route: Route) -> None:
        intercepted.put_nowait(route.request)
        await route.abort()

    await page.route("**/threads/*/commands", lose_request, times=1)
    composer = page.get_by_placeholder("Enter sends, Ctrl+Enter for a new line")
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
    assert await thread_browser.store.events(thread.id, limit=100) == source.entries[:4]

    await page.reload()
    await expect(page.get_by_text(command.submit_input.text, exact=True)).to_be_visible()
    await expect(page.get_by_text("Awaiting saved confirmation", exact=True)).to_be_visible()
    await expect(page.locator(".agentplane-user-bubble")).to_have_count(0)
    async with page.expect_request(original.url) as retried:
        await page.get_by_role("button", name="Retry", exact=True).click()
    retry = await retried.value
    assert retry.post_data is not None
    assert json_format.Parse(retry.post_data, command_pb2.Command()) == command
    async with asyncio.timeout(15):
        assert await source.commands.get() == command
    await expect(page.get_by_text("Saved · awaiting effect", exact=True)).to_have_count(1)
    await expect(page.get_by_text("Awaiting saved confirmation", exact=True)).to_have_count(0)
    await expect(page.locator(".agentplane-user-bubble")).to_have_count(0)
    admissions = [
        entry.event.command_admitted.command
        for entry in await thread_browser.store.events(thread.id, limit=100)
        if entry.event.HasField("command_admitted")
    ]
    assert admissions == [command]


async def test_streamed_admission_survives_a_lost_http_reply_and_reload(thread_browser: ThreadBrowser) -> None:
    page, source = thread_browser.page, thread_browser.source
    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    replies: asyncio.Queue[APIResponse] = asyncio.Queue()
    drop_reply = asyncio.Event()

    async def hold_reply(route: Route) -> None:
        # This is a real response from the app after PostgreSQL admission commit. Only its
        # delivery to this browser is withheld; the independent SSE stream remains connected.
        replies.put_nowait(await route.fetch())
        await drop_reply.wait()
        await route.abort()

    await page.route("**/threads/*/commands", hold_reply, times=1)
    try:
        composer = page.get_by_placeholder("Enter sends, Ctrl+Enter for a new line")
        await composer.fill("Test input saved without its HTTP reply")
        await composer.press("Enter")
        async with asyncio.timeout(15):
            response = await replies.get()
            command = await source.commands.get()
        assert response.status == 200
        admission = json_format.Parse(await response.text(), event_log_pb2.EventEntry())
        assert admission.event.command_admitted.command == command
        (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
        assert admission in await thread_browser.store.events(thread.id, limit=100)
        await expect(page.get_by_text("Saved · awaiting effect", exact=True)).to_have_count(1)
        await expect(page.locator(".agentplane-user-bubble")).to_have_count(0)

        async with page.expect_event("requestfailed", predicate=lambda request: request.url == response.url):
            drop_reply.set()
        await expect(page.get_by_text("Saved · awaiting effect", exact=True)).to_have_count(1)
        await expect(page.get_by_text("Awaiting saved confirmation", exact=True)).to_have_count(0)
        await page.reload()
        await expect(page.get_by_text(command.submit_input.text, exact=True)).to_have_count(1)
        await expect(page.get_by_text("Saved · awaiting effect", exact=True)).to_have_count(1)
        await expect(page.get_by_role("button", name="Retry", exact=True)).to_have_count(0)
        await expect(page.locator(".agentplane-user-bubble")).to_have_count(0)
    finally:
        drop_reply.set()
        await page.unroute_all(behavior="wait")


async def show_raw(page: Page) -> None:
    await page.get_by_role("button", name="More", exact=True).click()
    await page.get_by_role("menuitem", name="Raw frames", exact=True).click()
    await page.keyboard.press("Escape")


async def expect_raw_prefix(page: Page, cursor: int) -> None:
    await expect(page.locator(".agentplane-frame-sequence")).to_have_text(
        [f"Event {sequence} ·" for sequence in range(1, cursor + 1)]
    )


@pytest.mark.parametrize("replay_after", [4])
async def test_unobserved_committed_admission_reconciles_once_after_reload(thread_browser: ThreadBrowser) -> None:
    page, source, app = thread_browser.page, thread_browser.source, thread_browser.app
    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    await show_raw(page)
    replies: asyncio.Queue[APIResponse] = asyncio.Queue()
    drop_reply = asyncio.Event()

    async def lose_committed_reply(route: Route) -> None:
        replies.put_nowait(await route.fetch())
        await drop_reply.wait()
        await route.abort()

    await page.route("**/threads/*/commands", lose_committed_reply, times=1)
    try:
        composer = page.get_by_placeholder("Enter sends, Ctrl+Enter for a new line")
        await composer.fill("Test input whose admission neither browser channel observed")
        await composer.press("Enter")
        async with asyncio.timeout(15):
            response = await replies.get()
            command = await source.commands.get()
            assert (await app.replay_held()).cursor == 5
        assert response.status == 200
        admission = json_format.Parse(await response.text(), event_log_pb2.EventEntry())
        assert admission.event.command_admitted.command == command
        assert admission == source.entries[4]
        (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
        assert await thread_browser.store.events(thread.id, limit=100) == source.entries

        async with page.expect_event("requestfailed", predicate=lambda request: request.url == response.url):
            drop_reply.set()
        pending = page.get_by_role("region", name="Pending commands")
        await expect(pending.locator("[data-command-id]")).to_have_attribute("data-command-id", command.command_id)
        await expect(pending.get_by_text("Awaiting saved confirmation", exact=True)).to_be_visible()
        await expect(page.locator(".agentplane-user-bubble")).to_have_count(0)
        await expect_raw_prefix(page, 4)

        # Reload abandons the old, held SSE response. The new document must retain the same
        # local Command while replay is still held, despite the app already archiving admission.
        await page.reload()
        async with asyncio.timeout(15):
            assert (await app.replay_held()).cursor == 5
        await expect(pending.locator("[data-command-id]")).to_have_attribute("data-command-id", command.command_id)
        await expect(pending.get_by_text(command.submit_input.text, exact=True)).to_be_visible()
        await expect(pending.get_by_text("Awaiting saved confirmation", exact=True)).to_be_visible()
        await expect(page.get_by_text("Catching up: 4 / 5 events", exact=True)).to_be_visible()
        await expect(page.get_by_role("button", name="Retry", exact=True)).to_be_disabled()
        await expect_raw_prefix(page, 4)

        app.release_replay()
        await expect_raw_prefix(page, 5)
        await expect(pending.get_by_text("Saved · awaiting effect", exact=True)).to_be_visible()
        await expect(pending.get_by_text("Awaiting saved confirmation", exact=True)).to_have_count(0)
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
        await expect(page.locator(".agentplane-user-bubble")).to_have_text(command.submit_input.text)
        await expect_raw_prefix(page, 6)
        await expect(pending).to_have_count(0)
        assert source.commands.empty(), "reload/replay must not automatically send the Command again"
        archived = await thread_browser.store.events(thread.id, limit=100)
        assert archived == source.entries
        assert [entry for entry in archived if entry.event.HasField("command_admitted")] == [admission]
    finally:
        drop_reply.set()
        await page.unroute_all(behavior="wait")


@pytest.mark.parametrize("replay_after", [4])
async def test_http_admission_ahead_of_replay_does_not_skip_earlier_events(thread_browser: ThreadBrowser) -> None:
    page, source, app = thread_browser.page, thread_browser.source, thread_browser.app
    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    await show_raw(page)
    for text in (" and preceding delta A", " and preceding delta B"):
        source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="test-browser-item", text=text)))
    async with asyncio.timeout(15):
        assert (await app.replay_held()).cursor == 5

    composer = page.get_by_placeholder("Enter sends, Ctrl+Enter for a new line")
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
    assert await thread_browser.store.events(thread.id, limit=100) == source.entries

    pending = page.get_by_role("region", name="Pending commands")
    await expect(pending.get_by_text("Saved · replay catching up", exact=True)).to_be_visible()
    await expect(pending.locator("[data-command-id]")).to_have_attribute("data-command-id", command.command_id)
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    await expect(page.locator(".agentplane-user-bubble")).to_have_count(0)
    await expect_raw_prefix(page, 4)
    await expect(page.get_by_text("Operational runner snapshot", exact=False)).to_contain_text("consumed event 4")

    app.release_replay()
    await expect_raw_prefix(page, 7)
    await expect(
        page.get_by_text("Test retained prefix and preceding delta A and preceding delta B", exact=True)
    ).to_have_count(1)
    await expect(pending.get_by_text("Saved · awaiting effect", exact=True)).to_be_visible()
    await expect(pending.get_by_text("Saved · replay catching up", exact=True)).to_have_count(0)
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
    await expect(page.locator(".agentplane-user-bubble")).to_have_text(command.submit_input.text)
    await expect_raw_prefix(page, 8)
    await expect(pending).to_have_count(0)
    assert source.commands.empty()
    assert await thread_browser.store.events(thread.id, limit=100) == source.entries


@pytest.mark.parametrize("replay_after", [4])
async def test_a_dropped_stream_reopens_an_unconfirmed_command_without_reloading(thread_browser: ThreadBrowser) -> None:
    page, source, app = thread_browser.page, thread_browser.source, thread_browser.app
    thread_browser.opened.replay.set()
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    await show_raw(page)
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
        composer = page.get_by_placeholder("Enter sends, Ctrl+Enter for a new line")
        await composer.fill("Test input pending across a reconnect")
        await composer.press("Enter")
        async with asyncio.timeout(15):
            response = await replies.get()
            command = await source.commands.get()
            assert (await app.replay_held()).cursor == 5
        assert response.status == 200
        admission = json_format.Parse(await response.text(), event_log_pb2.EventEntry())
        assert admission == source.entries[4]
        assert admission.event.command_admitted.command == command
        async with page.expect_event("requestfailed", predicate=lambda request: request.url == response.url):
            drop_reply.set()
        pending = page.get_by_role("region", name="Pending commands")
        await expect(pending.get_by_text("Awaiting saved confirmation", exact=True)).to_be_visible()
        await expect_raw_prefix(page, 4)

        # Only the response transport ends. The document is not replaced, so the page itself has
        # to reopen the stream, and from the prefix it verified rather than from the start.
        async with page.expect_request(lambda request: request.url.endswith(FOLLOW_EVENTS_PATH)) as reconnecting:
            app.disconnect_replay()
            for text in (" and disconnected delta A", " and disconnected delta B"):
                source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="test-browser-item", text=text)))
        reopened = (await reconnecting.value).post_data_buffer
        assert reopened is not None
        assert _follow_from(reopened) == 4
        async with asyncio.timeout(15):
            assert (await app.replay_held()).cursor == 5
        assert await document.evaluate("original => original === document")
        await expect(pending.locator("[data-command-id]")).to_have_attribute("data-command-id", command.command_id)
        await expect(pending.get_by_text("Awaiting saved confirmation", exact=True)).to_be_visible()
        await expect_raw_prefix(page, 4)
        await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()

        app.release_replay()
        await expect_raw_prefix(page, 7)
        await expect(
            page.get_by_text("Test retained prefix and disconnected delta A and disconnected delta B", exact=True)
        ).to_have_count(1)
        await expect(pending.get_by_text("Saved · awaiting effect", exact=True)).to_be_visible()
        source.append(
            event_pb2.Event(
                harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
                    harness_message_id="test-input-after-eventsource-reconnect",
                    origin_command_ids=[command.command_id],
                    text=command.submit_input.text,
                    turn_id="test-browser-turn",
                )
            )
        )
        await expect(page.locator(".agentplane-user-bubble")).to_have_text(command.submit_input.text)
        await expect_raw_prefix(page, 8)
        await expect(pending).to_have_count(0)
        assert await document.evaluate("original => original === document")
        assert len(submissions) == 1
        assert source.commands.empty()
        (thread,) = await thread_browser.store.list_threads(sandbox=SANDBOX)
        assert await thread_browser.store.events(thread.id, limit=100) == source.entries
    finally:
        drop_reply.set()
        await page.unroute_all(behavior="wait")
        await document.dispose()


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
