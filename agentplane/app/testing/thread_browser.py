"""Fixtures and helpers shared by the real-browser thread tests, registered by each of them with
`pytest_plugins = ("agentplane.app.testing.thread_browser",)`.

A conftest fixture outranks a plugin's, so each test module defines `service_history_db_url` itself,
as `electric`'s database, for the package conftest's history fixtures to follow; and `sandbox` as the
`ReplicationSource`'s, whose scripted runner the Runner Gateway reaches.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from agentplane.app.testing import history_probe, thread_view_marks
from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.testing.http2_proxy import BrowserCertificate, Ingress, browser_certificate, http2_proxy
from agentplane.app.testing.replication_process import AppProcess, GatewayAccess, SandboxesAccess, app_process
from agentplane.app.testing.replication_source import (
    SANDBOX,
    SESSION,
    Opened,
    ReplicationSource,
    add_replication_source,
)
from agentplane.app.testing.runner_gateway import RUNNER_AUDIENCE
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import ContentStore
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.testing.electric_service import (
    BrowserPostgres,
    ElectricService,
    browser_electric_service,
    browser_postgres_server,
)
from agentplane.history_service.testing.history import History
from agentplane.proto import event_log_pb2, event_pb2
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess
from agentplane.runner_gateway.testing.runner_connection import runner_connection
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from util.bazel.runfiles import get_required_path
from util.testing.frontend_visual import deterministic_browser_context, disable_animations_script
from util.testing.postgres import DatabaseNames
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.viewports import DESKTOP, Viewport

# gazelle:include_dep @pypi//protobuf


@pytest.fixture
def certificate(tmp_path: Path) -> BrowserCertificate:
    return browser_certificate(tmp_path / "tls")


@pytest.fixture
def viewport() -> Viewport:
    return DESKTOP


@pytest.fixture
async def page(
    request: pytest.FixtureRequest, certificate: BrowserCertificate, monkeypatch: pytest.MonkeyPatch, viewport: Viewport
) -> AsyncIterator[Page]:
    # Route.fetch runs in Playwright's Node driver, outside Chromium's SPKI trust setting.
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(certificate.directory / "certificate.pem"))
    async with (
        async_playwright() as playwright,
        # Font-pinned profile and raster flags: the screenshots here are review assets, and unpinned
        # hinting, LCD text and Skia opts render differently from one executor to the next.
        deterministic_browser_context(
            playwright,
            viewport=viewport.size,
            frozen_now_ms=None,
            extra_args=[f"--ignore-certificate-errors-spki-list={certificate.spki}"],
        ) as context,
    ):
        await context.add_init_script(disable_animations_script())
        capture_geometry = history_probe.enabled()
        await context.add_init_script(path=thread_view_marks.script_path())
        if capture_geometry:
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
            if capture_geometry:
                await history_probe.write_results(
                    opened, undeclared_outputs_dir() / f"{request.node.name}-history-probe.json"
                )
            await thread_view_marks.write(
                opened, undeclared_outputs_dir() / f"{request.node.name}-thread-view-marks.jsonl"
            )
            await context.tracing.stop(path=undeclared_outputs_dir() / f"{request.node.name}-trace.zip")


async def expect_projected_cursor(page: Page, cursor: int) -> None:
    await page.wait_for_function(
        """cursor => {
            const value = document.querySelector('[data-projection-cursor]')?.dataset.projectionCursor;
            return value !== undefined && BigInt(value) >= BigInt(cursor);
        }""",
        arg=str(cursor),
    )


@dataclass
class ThreadBrowser:
    page: Page
    source: ReplicationSource
    store: ThreadStore
    event_logs: SeededEventLogStore
    content: ContentStore
    history: History
    opened: Opened
    app: AppProcess
    ingress: Ingress

    async def start_replay(self) -> None:
        """Release the history copy and wait until the browser applies its current projection."""
        self.opened.replay.set()
        await expect_projected_cursor(self.page, self.source.entries[-1].cursor)


@pytest.fixture(scope="session")
def thread_browser_postgres() -> Iterator[BrowserPostgres]:
    with browser_postgres_server() as postgres:
        yield postgres


@pytest.fixture
async def electric(
    database_names: DatabaseNames, thread_browser_postgres: BrowserPostgres
) -> AsyncIterator[ElectricService]:
    async with browser_electric_service(thread_browser_postgres, database_names()) as service:
        yield service


@pytest.fixture
def gateway_access(gateway_service: GatewayProcess, app_token_file: Path) -> GatewayAccess:
    return GatewayAccess(target=gateway_service.target, token_file=app_token_file)


@asynccontextmanager
async def scripted_runner(
    source: ReplicationSource,
    sandbox: SandboxCluster,
    gateway_service: GatewayProcess,
    sandbox_control: SandboxControlClient,
) -> AsyncIterator[None]:
    """`source`'s scripted runner, connected to the Runner Gateway as `sandbox`'s runner."""
    async with runner_connection(
        gateway_service.target, await sandbox.pod_token(RUNNER_AUDIENCE), partial(add_replication_source, source)
    ):
        await gateway_service.wait_for_runner(sandbox_control, sandbox.destination)
        yield


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
    event_logs: SeededEventLogStore,
    content: ContentStore,
    thread_source: ReplicationSource,
    history: History,
    history_client: HistoryServiceClient,
    replay_after: int | None,
    electric: ElectricService,
    certificate: BrowserCertificate,
    sandbox: SandboxCluster,
    gateway_service: GatewayProcess,
    sandbox_control: SandboxControlClient,
    gateway_access: GatewayAccess,
) -> AsyncIterator[ThreadBrowser]:
    source = thread_source
    thread_id = await event_logs.seed(SANDBOX, SESSION)
    await store.rename(thread_id, "Browser thread")
    async with (
        scripted_runner(source, sandbox, gateway_service, sandbox_control),
        source.ingesting(history),
        app_process(
            db_url,
            history_client,
            sandboxes=SandboxesAccess(kubeconfig=sandbox.server.kubeconfig, namespace=sandbox.namespace),
            gateway=gateway_access,
            browser_login=True,
            replay_after=replay_after,
            electric_url=electric.url,
        ) as app,
        http2_proxy(app.url, certificate) as ingress,
    ):
        # The operator session the app's relay of the Dispatcher's watch requires.
        login = await page.request.post(f"{ingress.url}/test-login", params={"seconds": 3600})
        assert login.ok, await login.text()
        async with asyncio.timeout(30):
            opened = await source.opened.get()
            await page.goto(f"{ingress.url}/#/threads/{thread_id}")
        yield ThreadBrowser(page, source, store, event_logs, content, history, opened, app, ingress)


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
