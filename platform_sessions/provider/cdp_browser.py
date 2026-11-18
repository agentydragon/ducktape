"""Experimental ordinary Chromium process with guarded Playwright CDP attachment."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from contextlib import suppress
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, Route


def validate_options(options: Mapping[str, Any]) -> None:
    unsupported = options.keys() - {
        "args",
        "executable_path",
        "service_workers",
        "record_har_path",
        "record_har_content",
    }
    if unsupported:
        raise ValueError(f"unsupported CDP launch options: {sorted(unsupported)}")
    if options.get("service_workers") != "block":
        raise ValueError("CDP launch requires service_workers='block'")
    if set(options.get("args", ())) - {"--restore-last-session", "--no-proxy-server"}:
        raise ValueError("CDP launch supports only restore-last-session and no-proxy-server arguments")
    if "record_har_content" in options and "record_har_path" not in options:
        raise ValueError("record_har_content requires record_har_path")


class CdpBrowser:
    """Own the process independently of the CDP connection's disconnect semantics.

    No startup window delays tab restoration until after the HTTP and WebSocket guards
    are installed. This guards page traffic, not arbitrary Chromium process egress.
    Profiles with service-worker storage are refused before Chromium can run a worker.
    """

    def __init__(
        self,
        *,
        playwright: Playwright,
        profile_dir: Path,
        display: str,
        options: Mapping[str, Any],
        timeout_seconds: float,
        allow_viewer_page: Callable[[Page], bool],
    ) -> None:
        validate_options(options)
        self._playwright = playwright
        self._profile_dir = profile_dir
        self._display = display
        self._options = options
        self._timeout_seconds = timeout_seconds
        self._allow_viewer_page = allow_viewer_page
        self._process: subprocess.Popen[bytes] | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._stderr_thread: threading.Thread | None = None

    def __enter__(self) -> BrowserContext:
        if any(self._profile_dir.glob("*/Service Worker")):
            raise ValueError("CDP launch refuses profiles with service-worker storage; use a fresh profile and sign in")
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        self._process = subprocess.Popen(
            [
                self._options.get("executable_path") or self._playwright.chromium.executable_path,
                *self._options.get("args", ()),
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-startup-window",
                "--disable-background-networking",
                "--disable-extensions",
                "--disable-quic",
                "--disk-cache-size=16777216",
                "--media-cache-size=8388608",
                f"--user-data-dir={self._profile_dir}",
                "--remote-debugging-address=127.0.0.1",
                # Port zero enables navigator.webdriver in Chromium. Use a nonzero
                # port and trust only the endpoint emitted by our own process.
                f"--remote-debugging-port={port}",
            ],
            env={**os.environ, "DISPLAY": self._display},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            ready: Future[str] = Future()
            assert self._process.stderr is not None
            stderr = self._process.stderr

            def read_stderr() -> None:
                for line in stderr:
                    endpoint = re.fullmatch(
                        rb"DevTools listening on (ws://127\.0\.0\.1:"
                        + str(port).encode("ascii")
                        + rb"/devtools/browser/[a-zA-Z0-9-]+)\s*",
                        line.strip(),
                    )
                    if endpoint is not None and not ready.done():
                        ready.set_result(endpoint[1].decode("ascii"))
                if not ready.done():
                    ready.set_exception(RuntimeError("ordinary Chromium exited before CDP became ready"))

            # Continue draining after startup so Chromium cannot wedge on a full
            # stderr pipe. Browser diagnostics can contain credentials; discard them.
            self._stderr_thread = threading.Thread(target=read_stderr, name="chromium-stderr", daemon=True)
            self._stderr_thread.start()
            endpoint_url = ready.result(timeout=self._timeout_seconds)
            if self._process.poll() is not None:
                raise RuntimeError("ordinary Chromium exited before CDP attachment")
            self._browser = self._playwright.chromium.connect_over_cdp(
                endpoint_url, timeout=self._timeout_seconds * 1000
            )
            context = self._browser.contexts[0]
            self._context = context
            self.guard_context(context)
            if "record_har_path" in self._options:
                context.tracing.start_har(
                    self._options["record_har_path"], content=self._options.get("record_har_content"), mode="full"
                )
            return context
        except BaseException:
            self.stop_process()
            raise

    def guard_context(self, context: BrowserContext) -> None:
        # Playwright 1.62 uses this same registration block for service_workers='block'.
        # Existing worker storage was rejected before process launch.
        context.add_init_script(
            "if (navigator.serviceWorker) navigator.serviceWorker.register = async () => {"
            "throw new Error('Service Worker registration blocked'); };"
        )
        context.route("**/*", self._guard_request)
        context.route_web_socket("**/*", lambda route: route.close())

    def _guard_request(self, route: Route) -> None:
        request = route.request
        if request.service_worker is None and self._allow_viewer_page(request.frame.page):
            route.continue_()
        else:
            # A provider's explicitly allowed page routes use continue_(), which skips
            # this fallback guard. fallback() deliberately remains denied.
            route.abort()

    def __exit__(self, *exc: object) -> None:
        try:
            if self._context is not None and "record_har_path" in self._options:
                self._context.tracing.stop_har()
            if self._browser is not None and self._browser.is_connected():
                client = self._browser.new_browser_cdp_session()
                client.send("Browser.close")
                assert self._process is not None
                with suppress(subprocess.TimeoutExpired):
                    self._process.wait(timeout=3)
        finally:
            self.stop_process()

    def stop_process(self) -> None:
        """Also callable by teardown if the Playwright worker has wedged."""
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=3)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=3)
        if self._process is not None and self._process.stderr is not None:
            self._process.stderr.close()
