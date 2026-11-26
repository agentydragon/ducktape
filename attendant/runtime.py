"""Provider operations executed in a fresh, inert page in the owner's Chrome profile."""

from __future__ import annotations

import base64
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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


_FETCH = """async ({method, url, headers, body, referrer}) => {
    const options = {method, headers, credentials: "same-origin", mode: "same-origin", redirect: "manual",
                     cache: "no-store", signal: AbortSignal.timeout(20000)};
    if (referrer) options.referrer = referrer;
    if (method !== "GET" && method !== "HEAD") options.body = Uint8Array.from(atob(body), c => c.charCodeAt(0));
    const r = await fetch(url, options);
    const reader = r.body?.getReader();
    let size = 0;
    const chunks = [];
    if (reader) while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        size += value.length;
        if (size > 16 * 1024 * 1024) { await reader.cancel(); throw Error("response size"); }
        for (let i = 0; i < value.length; i += 16384)
            chunks.push(String.fromCharCode(...value.subarray(i, i + 16384)));
    }
    return {status: r.type === "opaqueredirect" ? 302 : r.status, body: btoa(chunks.join("")),
            content_type: r.headers.get("content-type") || "application/octet-stream",
            csrf: r.headers.get("x-csrf-token"), challenge: r.headers.get("cf-mitigated") === "challenge"};
}"""


class Tab:
    def __init__(self, page: Page, origin: str):
        self._page = page
        self._origin = origin
        self.csrf: str | None = None

    @classmethod
    @contextmanager
    def open(cls, context: BrowserContext, origin: str) -> Iterator[Tab]:
        page = context.new_page()
        try:
            shell = origin + "/.well-known/attendant-blank"

            def blank(route: Route) -> None:
                if (
                    route.request.url == shell
                    and route.request.is_navigation_request()
                    and route.request.method == "GET"
                ):
                    route.fulfill(content_type="text/html", body="<!doctype html>")
                else:
                    route.abort()

            page.route("**/*", blank)
            page.route_web_socket("**/*", lambda _: None)
            page.goto(shell, wait_until="commit", timeout=10000)
            yield cls(page, origin)
        finally:
            page.close()

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

        self._page.route("**/*", allow)
        try:
            raw = self._page.evaluate(
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
            self._page.unroute("**/*", allow)
        return Reply(raw["status"], base64.b64decode(raw["body"]), raw["content_type"], raw["challenge"], raw["csrf"])


class Provider(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def origin(self) -> str: ...
    @property
    def login_url(self) -> str: ...
    def allows(self, op: Operation) -> bool: ...
    def identity(self, tab: Tab) -> str: ...
    def request(self, tab: Tab, op: Operation, identity: str) -> Reply: ...
