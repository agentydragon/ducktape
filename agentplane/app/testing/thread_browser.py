"""Fixtures and helpers shared by the real-browser thread tests, registered by each of them with
`pytest_plugins = ("agentplane.app.testing.thread_browser",)`.

A conftest fixture outranks a plugin's, so each test module defines `service_history_db_url` itself,
as `electric`'s database, for the package conftest's history fixtures to follow; and `sandbox` as the
`ReplicationSource`'s, whose scripted runner the Runner Gateway reaches.
"""

import asyncio
import faulthandler
import json
import re
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, async_playwright
from python.runfiles import Runfiles

from agentplane.app.testing import history_probe, thread_view_marks
from agentplane.app.testing.browser_certificate import BrowserCertificate, browser_certificate
from agentplane.app.testing.history import SessionHistory
from agentplane.app.testing.http2_proxy import Ingress, http2_proxy
from agentplane.app.testing.replication_process import AppProcess, GatewayAccess, SandboxesAccess, app_process
from agentplane.app.testing.replication_source import (
    SANDBOX,
    SESSION,
    Opened,
    ReplicationSource,
    add_replication_source,
)
from agentplane.app.testing.thread_test_support import ThreadHTTP
from agentplane.history_service.testing.electric_service import (
    BrowserPostgres,
    ElectricService,
    browser_electric_service,
    browser_postgres_server,
)
from agentplane.history_service.testing.go_service import GoHistoryService
from agentplane.history_service.testing.history import History
from agentplane.proto import event_log_pb2, event_pb2, session_pb2
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.session_dispatcher.client import SandboxControlClient
from agentplane.session_dispatcher.dispatcher.api.session import session_dispatcher_pb2
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient, accepted_session
from agentplane.session_dispatcher.testing.gateway_process import GatewayProcess
from agentplane.session_dispatcher.testing.runner_server import runner_server
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from testing.frontend_visual.context import deterministic_browser_context, disable_animations_script
from testing.frontend_visual.viewports import DESKTOP, Viewport
from testing.postgres import DatabaseNames

# gazelle:include_dep @pypi//protobuf


def _record_diagnostic_error(path: Path, error: str) -> None:
    try:
        with path.open("a") as output:
            output.write(error[:1000] + "\n")
    except OSError:
        # Diagnostic I/O must not replace the browser startup result.
        pass


async def _before_deadline[T](operation: Awaitable[T], deadline: float) -> T:
    """Do not wait for a CDP command's cancellation cleanup past the diagnostic deadline."""
    task = asyncio.ensure_future(operation)
    task.add_done_callback(lambda finished: None if finished.cancelled() else finished.exception())
    try:
        finished, _ = await asyncio.wait({task}, timeout=max(0, deadline - time.monotonic()))
    except BaseException:
        task.cancel()
        raise
    if task not in finished:
        task.cancel()
        raise TimeoutError("CDP diagnostic deadline expired")
    return task.result()


@contextmanager
def _startup_diagnostics(path: Path) -> Iterator[None]:
    """Capture independently of asyncio if browser startup stops making progress.

    The traceback timer is native to CPython. The watchdog uses procps outside
    the Python event loop, so a blocked event loop cannot suppress the sample.
    Both are stopped before the browser fixture starts teardown.
    """
    done = threading.Event()
    stack_path = path.with_name(path.stem + "-python-stacks.txt")
    top_path = path.with_name(path.stem + "-top.txt")
    roles_path = path.with_name(path.stem + "-process-roles.txt")
    error_path = path.with_name(path.stem + "-watchdog-error.txt")

    def sample() -> None:
        try:
            if done.wait(8):
                return
            # Capture only process identity and Chromium's --type flag. Full command
            # lines may contain test credentials, so they are never written out.
            listing = subprocess.check_output(
                ["ps", "-e", "-o", "pid=,ppid=,stat=,wchan:24=,comm=,args="], text=True, timeout=1
            )
            if done.is_set():
                return
            processes = [line.split(maxsplit=5) for line in listing.splitlines()]
            browser_parents = {fields[1] for fields in processes if len(fields) == 6 and "chrome" in fields[4]}
            with roles_path.open("w") as output:
                output.write(f"capture_time_ns={time.time_ns()}\n")
                for fields in processes:
                    if len(fields) != 6:
                        continue
                    pid, ppid, state, wait_channel, command, arguments = fields
                    if not ("chrome" in command or "node" in command or "python" in command or pid in browser_parents):
                        continue
                    kind = re.search(r"(?:^|\s)--type=([^\s]+)", arguments)
                    role = kind.group(1) if kind else "browser-parent" if pid in browser_parents else "-"
                    output.write(f"{pid} {ppid} {state} {wait_channel} {command} {role}\n")
            if done.is_set():
                return
            with top_path.open("wb") as output:
                process = subprocess.Popen(
                    ["top", "-b", "-n", "8", "-d", "3", "-w", "160"], stdout=output, stderr=subprocess.STDOUT
                )
                try:
                    done.wait(24)
                finally:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        try:
                            process.wait(timeout=0.5)
                        except subprocess.TimeoutExpired:
                            _record_diagnostic_error(error_path, f"top PID {process.pid} did not exit after SIGKILL")
        except Exception as error:
            _record_diagnostic_error(error_path, f"watchdog: {type(error).__name__}: {error}")

    with stack_path.open("w") as stack_output:
        faulthandler.dump_traceback_later(8, repeat=True, file=stack_output)
        watchdog = threading.Thread(target=sample, name="browser-startup-diagnostics", daemon=True)
        watchdog.start()
        try:
            yield
        finally:
            try:
                faulthandler.cancel_dump_traceback_later()
            except Exception as error:
                _record_diagnostic_error(error_path, f"faulthandler cancel: {type(error).__name__}: {error}")
            done.set()
            watchdog.join(timeout=2)
            try:
                if watchdog.is_alive():
                    _record_diagnostic_error(error_path, "watchdog thread did not stop within two seconds")
                    # The thread may still hold these descriptors. Unlinking keeps
                    # late writes from escaping the artifact bound.
                    for artifact in (top_path, roles_path):
                        artifact.unlink(missing_ok=True)
                for artifact in (stack_path, top_path, roles_path):
                    if not artifact.exists():
                        continue
                    if artifact.stat().st_size == 0:
                        artifact.unlink()
                    elif artifact.stat().st_size > 256_000:
                        with artifact.open("r+b") as output:
                            output.truncate(256_000)
            except OSError as error:
                _record_diagnostic_error(error_path, f"artifact cleanup: {type(error).__name__}: {error}")


@pytest.fixture
def certificate(tmp_path: Path) -> Iterator[BrowserCertificate]:
    certificate = browser_certificate(tmp_path / "tls")
    try:
        yield certificate
    finally:
        certificate.port_reservation.close()


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
        await context.add_init_script(
            path=Runfiles.CreateOrRaise().root() / "ducktape/agentplane/app/testing/thread_page.js"
        )
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
    store: ThreadHTTP
    event_logs: SessionHistory
    content: SessionHistory
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
    return GatewayAccess(
        target=gateway_service.target,
        token_file=None,
        tls=gateway_service.client_tls,
        environment_runtime_ca=gateway_service.environment_service.ca,
    )


@asynccontextmanager
async def scripted_runner(
    source: ReplicationSource,
    sandbox: SandboxCluster,
    gateway_service: GatewayProcess,
    sandbox_control: SandboxControlClient,
    tmp_path: Path,
) -> AsyncIterator[None]:
    """`source`'s scripted Runtime, observed by Environments as `sandbox`'s Runtime."""
    async with runner_server(
        "127.0.0.1:9090",
        gateway_service.environment_service.issue_workload_tls(
            sandbox.environment_runtime_identity_uri, tmp_path / "environment-runtime-transport"
        ),
        partial(add_replication_source, source),
        boot_id=source.boot_id,
    ):
        await gateway_service.environment_service.wait_for_runner(sandbox_control, sandbox.destination)
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
def thread_browser_session(
    request: pytest.FixtureRequest,
    db_url: str,
    store: ThreadHTTP,
    event_logs: SessionHistory,
    content: SessionHistory,
    thread_source: ReplicationSource,
    history: History,
    history_service: GoHistoryService,
    replay_after: int | None,
    electric: ElectricService,
    certificate: BrowserCertificate,
    sandbox: SandboxCluster,
    gateway_service: GatewayProcess,
    sandbox_control: SandboxControlClient,
    gateway_access: GatewayAccess,
    session_dispatcher: SessionDispatcherClient,
    tmp_path: Path,
) -> Callable[[Page], AbstractAsyncContextManager[ThreadBrowser]]:
    @asynccontextmanager
    async def open_browser(page: Page) -> AsyncIterator[ThreadBrowser]:
        trace_path = undeclared_outputs_dir() / f"{request.node.name}-browser-open-{uuid4().hex}.jsonl"

        def record(phase: str, error: BaseException | None = None) -> None:
            with trace_path.open("a") as output:
                output.write(
                    json.dumps(
                        {"time_ns": time.time_ns(), "phase": phase, "error": type(error).__name__ if error else None}
                    )
                    + "\n"
                )

        thread_id = await event_logs.seed(SANDBOX, SESSION)
        await store.rename(thread_id, "Browser thread")
        async with (
            scripted_runner(thread_source, sandbox, gateway_service, sandbox_control, tmp_path),
            thread_source.ingesting(history),
            app_process(
                db_url,
                history_service,
                sandboxes=SandboxesAccess(kubeconfig=sandbox.server.kubeconfig, namespace=sandbox.namespace),
                gateway=gateway_access,
                browser_login=True,
                certificate=certificate,
                replay_after=replay_after,
                electric_url=electric.url,
            ) as app,
            http2_proxy(app.url, certificate) as ingress,
        ):
            exact_spec = session_pb2.SessionSpec()
            thread_source.attached.spec.placement.environment_hosted.environment.name = SANDBOX
            thread_source.attached.spec.placement.environment_hosted.environment.uid = sandbox.uid
            exact_spec.CopyFrom(thread_source.attached.spec)
            await session_dispatcher.unary(
                session_dispatcher.stub.CreateSession,
                session_dispatcher_pb2.CreateSessionRequest(session_id=str(thread_id), spec=exact_spec),
            )
            await accepted_session(session_dispatcher, thread_id)
            # The operator session the app's relay of the Dispatcher's watch requires.
            record("login_start")
            await app.login(page)
            record("login_done")
            profile_session = None
            navigation_started_at = None
            navigation_done = False
            try:
                with _startup_diagnostics(trace_path):
                    async with asyncio.timeout(30):
                        record("opened_wait_start")
                        opened = await thread_source.opened.get()
                        record("opened_wait_done")
                        profile_session = await page.context.new_cdp_session(page)
                        record("profile_start")
                        await profile_session.send("Profiler.enable")
                        await profile_session.send("Profiler.start")
                        record("profile_ready")
                        navigation_started_at = time.monotonic()
                        record("navigation_start")
                        try:
                            await page.goto(f"{ingress.url}/#/threads/{thread_id}")
                        except BaseException as error:
                            record("navigation_failed", error)
                            raise
                        record("navigation_done")
                        navigation_done = True
            finally:
                if profile_session is not None:
                    slow = navigation_started_at is not None and (
                        not navigation_done or time.monotonic() - navigation_started_at >= 8
                    )
                    profile_path = trace_path.with_name(trace_path.stem + "-renderer.cpuprofile")
                    error_path = trace_path.with_name(trace_path.stem + "-renderer-profile-error.txt")
                    cleanup_deadline = time.monotonic() + 5
                    try:
                        # The startup verdict is already decided. Allow at most five more
                        # seconds to drain a renderer that unblocks just after the deadline.
                        record("profile_stop_start")
                        profile = await _before_deadline(profile_session.send("Profiler.stop"), cleanup_deadline)
                        record("profile_stop_done")
                        if slow:
                            payload = json.dumps(profile["profile"]).encode()
                            if len(payload) > 5_000_000:
                                _record_diagnostic_error(
                                    error_path, f"renderer CPU profile exceeded 5 MB: {len(payload)} bytes"
                                )
                            else:
                                profile_path.write_bytes(payload)
                    except Exception as error:
                        _record_diagnostic_error(error_path, f"Profiler.stop: {type(error).__name__}: {error}")
                    finally:
                        try:
                            await _before_deadline(profile_session.detach(), cleanup_deadline)
                            record("profile_detach_done")
                        except Exception as error:
                            _record_diagnostic_error(error_path, f"CDP detach: {type(error).__name__}: {error}")
            yield ThreadBrowser(page, thread_source, store, event_logs, content, history, opened, app, ingress)

    return open_browser


@pytest.fixture
async def thread_browser(
    page: Page, thread_browser_session: Callable[[Page], AbstractAsyncContextManager[ThreadBrowser]]
) -> AsyncIterator[ThreadBrowser]:
    async with thread_browser_session(page) as browser:
        yield browser


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
