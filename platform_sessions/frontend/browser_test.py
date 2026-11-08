"""Exercise the compiled owner UI in Chromium under the gateway's current CSP."""

from __future__ import annotations

import base64
import json
import re
from asyncio import Event, wait_for
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

import pytest
import pytest_bazel
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from playwright.async_api import Browser, Page, expect
from python.runfiles import Runfiles

from util.testing.asgi import serve_app

pytest_plugins = ("util.playwright",)

_CSP = (
    "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


@pytest.fixture
async def dashboard(runfiles: Runfiles) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    frontend = Path(str(runfiles.root() / "ducktape/platform_sessions/frontend/dist"))
    state: dict[str, Any] = {
        "imports": [],
        "probes": [],
        "session_requests": {},
        "delay_first_session": False,
        "first_session_started": Event(),
        "release_first_session": Event(),
        "hold_import": False,
        "import_started": Event(),
        "release_import": Event(),
        "signed_out": False,
    }
    app = FastAPI()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if state["signed_out"] and request.url.path.startswith("/api/"):
            return Response(status_code=401)
        response: Response = await call_next(request)
        if request.url.path == "/":
            response.headers["Content-Security-Policy"] = _CSP
        return response

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(frontend / "status.html")

    @app.get("/main.css")
    def css() -> FileResponse:
        return FileResponse(frontend / "main.css", media_type="text/css")

    @app.get("/main.js")
    def javascript() -> FileResponse:
        return FileResponse(frontend / "main.js", media_type="application/javascript")

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/api/v1/instances")
    def instances() -> dict[str, Any]:
        return {
            "instances": [
                {
                    "ref": {"instanceId": instance, "providerId": provider},
                    "info": {"instructions": [{"text": "Copy the exported session.", "command": "tool export"}]},
                }
                for instance, provider in (("one", "alpha"), ("two", "alpha"), ("beta", "beta"))
            ]
        }

    @app.get("/api/v1/instances/{instance_id}/session")
    async def session(instance_id: str) -> dict[str, Any]:
        request_count = state["session_requests"].get(instance_id, 0) + 1
        state["session_requests"][instance_id] = request_count
        if state["delay_first_session"] and instance_id == "one" and request_count == 1:
            state["first_session_started"].set()
            await state["release_first_session"].wait()
        message = "Fresh status" if request_count > 1 and instance_id == "one" else "Connected"
        return {
            "snapshot": {
                "state": "SESSION_STATE_VERIFIED",
                "ownerPresentation": {"message": message},
                "importedAt": "2026-11-01T00:00:00Z",
                "lastProbe": {"outcome": "PROBE_OUTCOME_SUCCESS", "completedAt": "2026-11-01T01:00:00Z"},
            }
        }

    @app.post("/api/v1/instances/{instance_id}:probe")
    def probe(instance_id: str) -> dict[str, str]:
        state["probes"].append(instance_id)
        return {"ok": "true"}

    @app.post("/api/v1/instances/{instance_id}:importSession")
    async def import_session(instance_id: str, request: Request) -> JSONResponse:
        body = await request.json()
        candidate = json.loads(base64.b64decode(body["candidate"], validate=True))
        state["imports"].append((instance_id, candidate))
        if state["hold_import"]:
            state["import_started"].set()
            await state["release_import"].wait()
        response_case = candidate["session"].get("response_case")
        responses: dict[str, dict[str, Any]] = {
            "challenge": {"outcome": "IMPORT_OUTCOME_CHALLENGE_REQUIRED"},
            "unknown": {"outcome": "IMPORT_OUTCOME_UNKNOWN"},
            "unknown_invalid": {
                "outcome": "IMPORT_OUTCOME_UNKNOWN",
                "outcomeUnknown": {"reason": "UNKNOWN_OUTCOME_REASON_INVALID_RESPONSE"},
            },
            "unknown_http": {
                "outcome": "IMPORT_OUTCOME_UNKNOWN",
                "outcomeUnknown": {"reason": "UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR", "upstreamHttpStatus": 429},
            },
            "unknown_http_invalid": {
                "outcome": "IMPORT_OUTCOME_UNKNOWN",
                "outcomeUnknown": {"reason": "UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR", "upstreamHttpStatus": 999},
            },
            "timeout": {
                "outcome": "IMPORT_OUTCOME_UNKNOWN",
                "outcomeUnknown": {"reason": "UNKNOWN_OUTCOME_REASON_TIMEOUT"},
            },
            "rejected": {"outcome": "IMPORT_OUTCOME_REJECTED"},
            "reauth": {"outcome": "IMPORT_OUTCOME_REAUTHENTICATION_REQUIRED"},
            "unavailable": {"outcome": "IMPORT_OUTCOME_UPSTREAM_UNAVAILABLE"},
            "invalid_response": {
                "outcome": "IMPORT_OUTCOME_FAILURE",
                "failure": {"reason": "IMPORT_FAILURE_REASON_INVALID_RESPONSE"},
            },
            "storage_error": {
                "outcome": "IMPORT_OUTCOME_FAILURE",
                "failure": {"reason": "IMPORT_FAILURE_REASON_STORAGE_ERROR"},
            },
            "upstream_http": {
                "outcome": "IMPORT_OUTCOME_FAILURE",
                "failure": {"reason": "IMPORT_FAILURE_REASON_UPSTREAM_HTTP_ERROR", "upstreamHttpStatus": 429},
            },
            "future": {"outcome": "IMPORT_OUTCOME_FROM_FUTURE", "message": "must not be displayed"},
        }
        result = responses.get(response_case, {"outcome": "IMPORT_OUTCOME_INSTALLED"})
        result["message"] = "provider-internal response with private data"
        return JSONResponse(result)

    async with serve_app(app) as origin:
        try:
            yield origin, state
        finally:
            state["release_first_session"].set()
            state["release_import"].set()


def _listen_for_browser_errors(page: Page, errors: list[str]) -> None:
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: errors.append(str(error)))


async def _check_page_theme(browser: Browser, origin: str, scheme: Literal["light", "dark"]) -> tuple[str, str]:
    context = await browser.new_context(color_scheme=scheme, viewport={"width": 390, "height": 844})
    page = await context.new_page()
    errors: list[str] = []
    _listen_for_browser_errors(page, errors)
    response = await page.goto(origin, wait_until="domcontentloaded")
    assert response is not None
    assert response.headers.get("content-security-policy") == _CSP
    await expect(page.get_by_role("heading", name="Platform sessions")).to_be_visible()
    await expect(page.get_by_role("region", name="one integration")).to_be_visible()
    await page.wait_for_function(
        "expected => document.documentElement.getAttribute('data-mantine-color-scheme') === expected", arg=scheme
    )
    background, foreground = await page.evaluate(
        "[getComputedStyle(document.body).backgroundColor, getComputedStyle(document.body).color]"
    )
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    # React sets CSS properties through the DOM; assert actual rendering and CSP errors
    # rather than rejecting style attributes that Chromium allows when set this way.
    button = page.get_by_role("button", name="Verify and import")
    assert await button.evaluate("element => getComputedStyle(element).backgroundColor") != "rgba(0, 0, 0, 0)"
    assert await page.locator("style").count() == 0
    assert errors == [], f"browser errors under {scheme} color scheme: {errors}"
    await context.close()
    return background, foreground


async def test_owner_ui_behaviors_and_csp(browser: Browser, dashboard: tuple[str, dict[str, Any]]) -> None:
    origin, state = dashboard
    light = await _check_page_theme(browser, origin, "light")
    dark = await _check_page_theme(browser, origin, "dark")
    assert light != dark, f"system color preference produced the same page colors: {light}"

    context = await browser.new_context(color_scheme="light", viewport={"width": 390, "height": 844})
    page = await context.new_page()
    errors: list[str] = []
    _listen_for_browser_errors(page, errors)
    await page.goto(origin, wait_until="domcontentloaded")
    await expect(page.get_by_text("Connected").first).to_be_visible()

    card = page.get_by_role("region", name="one integration")
    await card.get_by_role("button", name="Export instructions").click()
    await expect(card.get_by_text("Copy the exported session.")).to_be_visible()
    assert await page.locator("style").count() == 0
    await card.get_by_role("button", name="Check again").click()
    assert state["probes"] == ["one"]

    export = json.dumps({"version": 1, "provider": "alpha", "session": {"access_token": "secret-session-token"}})
    session_input = page.get_by_label("Session export")
    await session_input.fill(export)
    await expect(page.get_by_text("Choose which alpha integration to replace.")).to_be_visible()
    await page.get_by_role("button", name="Verify and import").click()
    await expect(page.get_by_role("status")).to_contain_text("Choose which integration to replace")
    assert state["imports"] == []
    await page.get_by_label("Choose an integration").select_option("two")
    async with page.expect_response(lambda response: response.url.endswith("/two:importSession")):
        await page.get_by_role("button", name="Verify and import").click()
    await expect(page.get_by_role("status")).to_contain_text("two session imported")
    assert state["imports"][0] == ("two", json.loads(export))
    assert await session_input.input_value() == ""
    assert "secret-session-token" not in await page.locator("body").inner_text()

    cases = [
        ("challenge", "security challenge"),
        ("unknown", "outcome is unknown"),
        ("unknown_invalid", "unexpected response.*outcome is unknown"),
        ("unknown_http", "HTTP 429.*outcome is unknown"),
        ("unknown_http_invalid", "HTTP error.*outcome is unknown"),
        ("timeout", "timed out.*may have completed"),
        ("rejected", "session string is invalid"),
        ("reauth", "fresh sign-in"),
        ("unavailable", "Could not connect.*may have completed"),
        ("invalid_response", "unexpected response.*Check the session status"),
        ("storage_error", "session storage.*Check the session status"),
        ("upstream_http", "HTTP 429.*Check the session status"),
        ("future", "outcome is unknown"),
    ]
    for response_case, expected in cases:
        body = json.dumps({"version": 1, "provider": "beta", "session": {"response_case": response_case}})
        await session_input.fill(body)
        async with page.expect_response(lambda response: response.url.endswith("/beta:importSession")):
            await page.get_by_role("button", name="Verify and import").click()
        await expect(page.get_by_role("status")).to_contain_text(re.compile(expected))
        await expect(page.get_by_role("status")).not_to_contain_text("provider-internal")
        assert await session_input.input_value() == body

    disabled_provider = json.dumps({"version": 1, "provider": "not-enabled", "session": {"cookie": "private"}})
    previous_imports = len(state["imports"])
    await session_input.fill(disabled_provider)
    await expect(page.get_by_text("not-enabled is not enabled on this service.")).to_be_visible()
    await page.get_by_role("button", name="Verify and import").click()
    await expect(page.get_by_role("status")).to_contain_text("The session was not sent")
    assert len(state["imports"]) == previous_imports
    assert errors == [], f"browser errors during provider/import flow: {errors}"
    assert await page.locator("style").count() == 0
    await context.close()


async def test_status_refresh_race_and_import_lock(browser: Browser, dashboard: tuple[str, dict[str, Any]]) -> None:
    origin, state = dashboard
    state["delay_first_session"] = True
    context = await browser.new_context(color_scheme="light")
    page = await context.new_page()
    await page.goto(origin, wait_until="domcontentloaded")
    await wait_for(state["first_session_started"].wait(), timeout=10)

    card = page.get_by_role("region", name="one integration")
    async with page.expect_response(lambda response: response.url.endswith("/one/session")):
        await card.get_by_role("button", name="Check again").click()
    await expect(card.get_by_text("Fresh status")).to_be_visible()
    state["release_first_session"].set()
    await expect(card.get_by_text("Connected")).to_have_count(0)

    state["hold_import"] = True
    export = json.dumps({"version": 1, "provider": "beta", "session": {"cookie": "secret-cookie"}})
    session_input = page.get_by_label("Session export")
    await session_input.fill(export)
    async with page.expect_request(lambda request: request.url.endswith("/beta:importSession")):
        await page.get_by_role("button", name="Verify and import").click()
    await wait_for(state["import_started"].wait(), timeout=10)
    await page.get_by_label("Session export").evaluate("textarea => textarea.closest('form').requestSubmit()")
    assert len(state["imports"]) == 1
    state["release_import"].set()
    await expect(page.get_by_role("status")).to_contain_text("beta session imported")
    assert len(state["imports"]) == 1
    assert "secret-cookie" not in await page.locator("body").inner_text()
    await context.close()


async def test_unauthorized_api_response_redirects_to_login(
    browser: Browser, dashboard: tuple[str, dict[str, Any]]
) -> None:
    origin, state = dashboard
    context = await browser.new_context(color_scheme="light")
    page = await context.new_page()
    await page.goto(origin, wait_until="domcontentloaded")
    await expect(page.get_by_text("Connected").first).to_be_visible()
    state["signed_out"] = True
    async with page.expect_navigation(url="**/auth/login"):
        await page.get_by_role("button", name="Check again").first.click()
    assert page.url.endswith("/auth/login")
    await context.close()


if __name__ == "__main__":
    pytest_bazel.main()
