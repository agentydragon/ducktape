"""Provider operations executed in a fresh, inert page in the owner's Chrome profile."""

from __future__ import annotations

import base64
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from playwright.sync_api import BrowserContext, Page, Route


class SessionError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Operation:
    method: str
    target: str
    body: bytes = b""


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes
    content_type: str
    challenge: bool = False
    csrf: str | None = None


def valid_target(target: str) -> bool:
    """Keep browser URL normalization from changing the route the adapter authorized."""
    path = target.partition("?")[0]
    return (
        len(target) <= 8192
        and target.startswith("/")
        and "//" not in path
        and "%" not in path
        and not {"..", "."}.intersection(path.split("/"))
        and re.fullmatch(r"[!-~]+", target) is not None
        and not any(char in target for char in ("\\", "#"))
    )


_FETCH = Path(__file__).with_name("fetch.js").read_text(encoding="utf-8")


class Tab:
    def __init__(self, context: BrowserContext, origin: str):
        self._context = context
        self._origin = origin
        self._page: Page | None = None

    @classmethod
    @contextmanager
    def open(cls, context: BrowserContext, origin: str) -> Iterator[Tab]:
        tab = cls(context, origin)
        try:
            yield tab
        finally:
            if tab._page is not None:
                tab._page.close()

    def _request_page(self) -> Page:
        # A status probe that only inspects the owner's tab never opens a second page.
        if self._page is not None:
            return self._page
        page = self._context.new_page()
        self._page = page
        shell = self._origin + "/.well-known/attendant-blank"

        def blank(route: Route) -> None:
            if route.request.url == shell and route.request.is_navigation_request() and route.request.method == "GET":
                route.fulfill(content_type="text/html", body="<!doctype html>")
            else:
                route.abort()

        page.route("**/*", blank)
        page.route_web_socket("**/*", lambda _: None)
        page.goto(shell, wait_until="commit", timeout=10000)
        return page

    def fetch(
        self,
        method: str,
        target: str,
        body: bytes = b"",
        *,
        headers: dict[str, str] | None = None,
        referrer: str | None = None,
    ) -> Reply:
        if not valid_target(target):
            raise SessionError("denied")
        url = self._origin + target

        def allow(route: Route) -> None:
            if route.request.method == method and route.request.url == url and route.request.resource_type == "fetch":
                route.continue_()
            else:
                route.abort()

        page = self._request_page()
        page.route("**/*", allow)
        try:
            raw = page.evaluate(
                _FETCH,
                {
                    "method": method,
                    "url": url,
                    "headers": headers or {},
                    "referrer": referrer,
                    "body": base64.b64encode(body).decode("ascii"),
                },
            )
        finally:
            page.unroute("**/*", allow)
        return Reply(raw["status"], base64.b64decode(raw["body"]), raw["content_type"], raw["challenge"], raw["csrf"])


class Provider(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def origin(self) -> str: ...
    @property
    def login_url(self) -> str: ...
    def allows(self, op: Operation) -> bool: ...
    def check_session(self, tab: Tab, page: Page | None) -> None:
        """Inspect Chrome and raise SessionError if the session needs attention."""
        ...

    def request(self, tab: Tab, op: Operation) -> Reply: ...
