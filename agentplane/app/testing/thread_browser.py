"""Fixtures and helpers shared by the real-browser thread tests, registered by each of them with
`pytest_plugins = ("agentplane.app.testing.thread_browser",)`.

A conftest fixture outranks a plugin's, so each test module defines `db_url` itself, as `electric`'s
database, for the package conftest's stores to follow.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from agentplane.app.testing import history_probe, history_trace
from agentplane.app.testing.electric_service import ElectricService, electric_service
from agentplane.app.testing.http2_proxy import BrowserCertificate, Ingress, browser_certificate, http2_proxy
from agentplane.app.testing.replication_process import AppProcess, app_process
from agentplane.app.testing.replication_source import SANDBOX, SESSION, Opened, ReplicationSource
from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import ContentStore
from agentplane.protocol import event_log_pb2, event_pb2
from util.bazel.runfiles import get_required_path
from util.testing.frontend_visual import CONTAINER_BASE_BROWSER_ARGS, chromium_executable
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf


@pytest.fixture
def certificate(tmp_path: Path) -> BrowserCertificate:
    return browser_certificate(tmp_path / "tls")


@pytest.fixture
async def page(
    request: pytest.FixtureRequest, certificate: BrowserCertificate, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Page]:
    # Route.fetch runs in Playwright's Node driver, outside Chromium's SPKI trust setting.
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(certificate.directory / "certificate.pem"))
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=chromium_executable(),
            args=[*CONTAINER_BASE_BROWSER_ARGS, f"--ignore-certificate-errors-spki-list={certificate.spki}"],
        )
        try:
            async with await browser.new_context(viewport={"width": 1280, "height": 900}) as context:
                await context.add_init_script(path=history_probe.script_path())
                await context.add_init_script(path=get_required_path("_main/agentplane/app/testing/thread_page.js"))
                await context.tracing.start(screenshots=True, snapshots=True, sources=True)
                opened = await context.new_page()
                await history_probe.throttle_cpu(context, opened)
                errors: list[str] = []
                opened.on("pageerror", lambda error: errors.append(str(error)))
                try:
                    yield opened
                    assert not errors, errors
                finally:
                    await history_probe.write_results(
                        opened, undeclared_outputs_dir() / f"{request.node.name}-history-probe.json"
                    )
                    await history_trace.write(
                        opened, undeclared_outputs_dir() / f"{request.node.name}-history-trace.jsonl"
                    )
                    await context.tracing.stop(path=undeclared_outputs_dir() / f"{request.node.name}-trace.zip")
        finally:
            await browser.close()


@dataclass
class ThreadBrowser:
    page: Page
    source: ReplicationSource
    store: ThreadStore
    event_logs: EventLogStore
    content: ContentStore
    opened: Opened
    app: AppProcess
    ingress: Ingress


@pytest.fixture
async def electric() -> AsyncIterator[ElectricService]:
    async with electric_service() as service:
        yield service


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
    page: Page,
    db_url: str,
    store: ThreadStore,
    event_logs: EventLogStore,
    content: ContentStore,
    thread_source: ReplicationSource,
    replay_after: int | None,
    electric: ElectricService,
    certificate: BrowserCertificate,
) -> AsyncIterator[ThreadBrowser]:
    source = thread_source
    thread_id = await event_logs.open(SANDBOX, SESSION, source.attached.spec)
    await store.rename(thread_id, "Browser thread")
    directory = get_required_path("_main/agentplane/app/frontend/dist/index.html").parent
    async with (
        source.serve() as runner_port,
        app_process(
            db_url, runner_port, frontend_directory=directory, replay_after=replay_after, electric_url=electric.url
        ) as app,
        http2_proxy(app.url, certificate) as ingress,
    ):
        async with asyncio.timeout(30):
            opened = await source.opened.get()
            await page.goto(f"{ingress.url}/#/threads/{thread_id}")
        yield ThreadBrowser(page, source, store, event_logs, content, opened, app, ingress)


def append_items(thread_browser: ThreadBrowser, prefix: str, numbers: range) -> event_log_pb2.EventEntry:
    """One finished assistant message per number, of varying height; the entry of the last."""
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


def message_composer(page: Page) -> Locator:
    return page.get_by_role("textbox", name="Message", exact=True)


async def frames(page: Page) -> None:
    """Waits for the paint after the next layout, and any effect or observer it runs."""
    await page.evaluate("() => window.__threadPage.frames()")


async def capture_reading_anchor(area: Locator) -> dict[str, str | float]:
    """The first row whose bottom is below the viewport top, and its position -- the reader's
    place, sampled once two consecutive frames agree so a pending re-measure right after a
    just-ended gesture cannot register as a false position."""
    return cast("dict[str, str | float]", await area.evaluate("area => window.__threadPage.settledAnchor(area)"))


async def wheel_and_capture_anchor_at_scrollend(page: Page, area: Locator, delta_y: float) -> dict[str, str | float]:
    """Wheel `area` and return the reader's place as the gesture's scrollend event sees it -- the
    instant the app adopts it. capture_reading_anchor samples frames later, which is too late when
    older pages land right after the scrollend: it can catch the position part-way through the
    scroll restorations they trigger, not the one the reader left."""
    gesture = await area.evaluate_handle("area => window.__threadPage.anchorAtScrollend(area)")
    await page.mouse.wheel(0, delta_y)
    async with asyncio.timeout(30):
        anchor = cast("dict[str, str | float]", await gesture.evaluate("gesture => gesture.anchor"))
    await gesture.dispose()
    return anchor


async def expect_reading_anchor(page: Page, anchor: dict[str, str | float]) -> None:
    try:
        await page.wait_for_function(
            """anchor => {
            const area = document.querySelector('[aria-label="Thread history"]');
            const item = area.querySelector(`[data-thread-anchor="${anchor.cursor}"]`);
            return item !== null && Math.abs(
                item.getBoundingClientRect().top - area.getBoundingClientRect().top - anchor.offset
            ) <= 2;
        }""",
            arg=anchor,
        )
    except PlaywrightTimeoutError:
        geometry = await page.evaluate(
            """expected => {
                const area = document.querySelector('[aria-label="Thread history"]');
                const top = area.getBoundingClientRect().top;
                return {
                    expected,
                    scrollTop: area.scrollTop,
                    scrollHeight: area.scrollHeight,
                    viewportHeight: area.clientHeight,
                    viewportWidth: area.clientWidth,
                    rows: [...area.querySelectorAll('[data-thread-anchor]')].map(item => ({
                        cursor: item.dataset.threadAnchor,
                        offset: item.getBoundingClientRect().top - top,
                        height: item.getBoundingClientRect().height,
                    })),
                };
            }""",
            anchor,
        )
        thread_id = urlsplit(page.url).fragment.split("/")[-1]
        (undeclared_outputs_dir() / f"reading-anchor-{thread_id}.json").write_text(json.dumps(geometry, indent=2))
        raise
