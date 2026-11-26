"""Exercise Attendant's session boundaries in Chromium against synthetic sites."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4

import pytest
import pytest_bazel
from playwright.sync_api import BrowserContext, expect, sync_playwright

from attendant.host import Session
from attendant.runtime import Operation, Reply, SessionError, Tab
from testing.browser.launcher import CONTAINER_BASE_BROWSER_ARGS, chromium_executable

_LOGIN_FORM = b"""<!doctype html><title>Fixture sign-in</title><link rel="icon" href="data:,">
<form method="post">
    <button name="account" value="fixture-alice">Sign in as Alice</button>
    <button name="account" value="fixture-bob">Sign in as Bob</button>
</form>"""
_READ_NOTES = Operation("GET", "/api/notes", b"")


@dataclass
class _FixtureSite:
    origin: str
    off_origin_redirect: str = ""
    requests: list[tuple[str, str]] = field(default_factory=list)
    sessions: dict[str, str] = field(default_factory=dict)
    expire_on_read: bool = False


@contextmanager
def _serve_site() -> Iterator[_FixtureSite]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            site.requests.append((self.command, self.path))
            if self.path == "/login":
                self._respond(200, _LOGIN_FORM, ("Content-Type", "text/html"))
                return

            if self.path == "/api/notes" and site.expire_on_read:
                site.sessions.clear()
                site.expire_on_read = False
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            token = cookies.get("fixture_session")
            account = site.sessions.get(token.value) if token else None
            if account is None:
                self._respond(401, b"Sign in required")
            elif self.path == "/api/identity":
                self._respond(200, account.encode())
            elif self.path == "/api/notes":
                self._respond(200, f"Fixture note owned by {account}".encode())
            elif self.path == "/api/redirect-away":
                self._respond(302, b"", ("Location", site.off_origin_redirect))
            elif self.path == "/api/redirect-private":
                self._respond(302, b"", ("Location", "/api/unlisted"))
            else:
                self._respond(403, b"Unlisted route")

        def do_POST(self) -> None:
            site.requests.append((self.command, self.path))
            if self.path != "/login":
                self._respond(403, b"Unlisted route")
                return
            body = self.rfile.read(int(self.headers["Content-Length"]))
            (account,) = parse_qs(body.decode())["account"]
            if account not in {"fixture-alice", "fixture-bob"}:
                self._respond(400, b"Unknown fixture account")
                return
            token = uuid4().hex
            site.sessions[token] = account
            self._respond(
                200,
                _LOGIN_FORM + f'<p role="status">Signed in as {account}</p>'.encode(),
                ("Content-Type", "text/html"),
                ("Set-Cookie", f"fixture_session={token}; Path=/; Max-Age=3600; HttpOnly; SameSite=Lax"),
            )

        def _respond(self, status: int, body: bytes, *headers: tuple[str, str]) -> None:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        site = _FixtureSite(f"http://127.0.0.1:{server.server_port}")
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            yield site
        finally:
            server.shutdown()
            worker.join()


@dataclass(frozen=True)
class _FixtureProvider:
    origin: str
    name: str = "fixture"

    @property
    def login_url(self) -> str:
        return self.origin + "/login"

    def allows(self, op: Operation) -> bool:
        return (
            op.method == "GET"
            and not op.body
            and op.target in {"/api/notes", "/api/redirect-away", "/api/redirect-private"}
        )

    def identity(self, tab: Tab) -> str:
        reply = tab.fetch("GET", "/api/identity")
        if reply.status == 401:
            raise SessionError("sign_in_needed")
        return reply.body.decode()

    def request(self, tab: Tab, op: Operation, identity: str) -> Reply:
        reply = tab.fetch(op.method, op.target, op.body)
        if reply.status == 401:
            raise SessionError("sign_in_needed")
        return reply


def _sign_in(session: Session, context: BrowserContext, provider: _FixtureProvider, account: str) -> None:
    session.open(provider.name)
    (page,) = (page for page in context.pages if page.url == provider.login_url)
    page.get_by_role("button", name=f"Sign in as {account.title()}").click()
    expect(page.get_by_role("status")).to_have_text(f"Signed in as fixture-{account}")


def _expect_issue(session: Session, provider: _FixtureProvider, op: Operation, code: str) -> None:
    with pytest.raises(SessionError) as caught:
        session.request(provider.name, op)
    assert caught.value.code == code


def test_browser_sign_in_binding_expiry_and_restart(tmp_path: Path) -> None:
    with sync_playwright() as playwright, _serve_site() as site, _serve_site() as other_site:
        site.off_origin_redirect = other_site.origin + "/external"
        provider = _FixtureProvider(site.origin)
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        profile = tmp_path / "chromium"
        with playwright.chromium.launch_persistent_context(
            profile, executable_path=chromium_executable(), args=CONTAINER_BASE_BROWSER_ARGS, service_workers="block"
        ) as context:
            session = Session(context, state_dir, [provider])
            assert session.status(provider.name) == "sign_in_needed"
            _expect_issue(session, provider, _READ_NOTES, "sign_in_needed")

            _sign_in(session, context, provider, "alice")
            assert session.status(provider.name) == "binding_required"
            _expect_issue(session, provider, _READ_NOTES, "binding_required")
            assert ("GET", _READ_NOTES.target) not in site.requests
            assert session.status(provider.name, bind=True) == "ready"
            assert session.request(provider.name, _READ_NOTES).body == b"Fixture note owned by fixture-alice"

            for op in (
                Operation("GET", site.off_origin_redirect, b""),
                Operation("GET", site.off_origin_redirect.removeprefix("http:"), b""),
                Operation("GET", "/api/unlisted", b""),
                Operation("POST", _READ_NOTES.target, b""),
            ):
                _expect_issue(session, provider, op, "denied")
            for target in ("/api/redirect-away", "/api/redirect-private"):
                assert session.request(provider.name, Operation("GET", target)).status == 302
                assert ("GET", target) in site.requests
            assert not other_site.requests
            assert ("GET", "/api/unlisted") not in site.requests
            assert ("POST", _READ_NOTES.target) not in site.requests

            site.expire_on_read = True
            _expect_issue(session, provider, _READ_NOTES, "sign_in_needed")
            assert session.status(provider.name) == "sign_in_needed"
            _sign_in(session, context, provider, "alice")
            assert session.status(provider.name) == "ready"
            assert session.request(provider.name, _READ_NOTES).body == b"Fixture note owned by fixture-alice"

        with playwright.chromium.launch_persistent_context(
            profile, executable_path=chromium_executable(), args=CONTAINER_BASE_BROWSER_ARGS, service_workers="block"
        ) as context:
            session = Session(context, state_dir, [provider])
            assert session.status(provider.name) == "ready"
            assert session.request(provider.name, _READ_NOTES).body == b"Fixture note owned by fixture-alice"

            _sign_in(session, context, provider, "bob")
            assert session.status(provider.name, bind=True) == "account_changed"
            reads_before = site.requests.count(("GET", _READ_NOTES.target))
            _expect_issue(session, provider, _READ_NOTES, "account_changed")
            assert site.requests.count(("GET", _READ_NOTES.target)) == reads_before


if __name__ == "__main__":
    pytest_bazel.main()
