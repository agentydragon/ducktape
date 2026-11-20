"""Exercise the compiled owner UI in Chromium under the gateway's current CSP."""

from __future__ import annotations

import base64
import json
import re
import struct
from asyncio import Event, wait_for
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

import pytest
import pytest_bazel
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image
from playwright.async_api import Browser, Page, Playwright, expect
from python.runfiles import Runfiles

from devinfra.testing.output_writer import BoundTestOutputs
from testing.asgi import serve_app
from testing.frontend_visual.capture import open_visual_page
from testing.frontend_visual.context import deterministic_browser_context

pytest_plugins = ("util.playwright",)

_CSP = (
    "default-src 'none'; script-src 'self'; connect-src 'self' ws:; style-src 'self'; "
    "img-src data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


async def _wait_for_remote_pixels(page: Page) -> None:
    await page.wait_for_function(
        """() => {
            const display = document.querySelector('[aria-label="one remote display"]');
            const canvas = display?.querySelector('canvas');
            if (!canvas || canvas.width < 640 || canvas.height < 480) return false;
            const displayBox = display.getBoundingClientRect();
            const canvasBox = canvas.getBoundingClientRect();
            if (displayBox.width < 280 || displayBox.height < 200 ||
                canvasBox.width < 240 || canvasBox.height < 170 ||
                canvasBox.left < displayBox.left - 2 || canvasBox.right > displayBox.right + 2 ||
                canvasBox.top < displayBox.top - 2 || canvasBox.bottom > displayBox.bottom + 2 ||
                canvasBox.top < 0 || canvasBox.bottom > innerHeight) return false;
            const context = canvas.getContext('2d');
            const header = context.getImageData(10, 10, 1, 1).data;
            const button = context.getImageData(250, 320, 1, 1).data;
            return header[0] === 29 && header[1] === 78 && header[2] === 216 &&
                button[0] === 37 && button[1] === 99 && button[2] === 235;
        }""",
        timeout=10_000,
    )


async def _wait_for_compressed_pixels(page: Page) -> None:
    await page.wait_for_function(
        """() => {
            const canvas = document.querySelector('[aria-label="one remote display"] canvas');
            if (!canvas) return false;
            const context = canvas.getContext('2d');
            const jpeg = context.getImageData(50, 150, 1, 1).data;
            const png = context.getImageData(580, 150, 1, 1).data;
            return jpeg[0] > 180 && jpeg[1] < 50 && jpeg[2] < 60 &&
                png[0] < 50 && png[1] > 160 && png[2] < 110;
        }""",
        timeout=10_000,
    )


@pytest.fixture
async def dashboard(runfiles: Runfiles) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    frontend = Path(str(runfiles.root() / "ducktape/platform_sessions/frontend/dist"))
    state: dict[str, Any] = {
        "instances": (("one", "alpha"), ("two", "alpha"), ("beta", "beta")),
        "instance_info": {},
        "imports": [],
        "probes": [],
        "probe_outcome": {"verified": {}},
        "metadata_requests": 0,
        "metadata_failures_remaining": 0,
        "instructions": [{"text": "Copy the exported session.", "command": "tool export"}],
        "session_requests": {},
        "delay_first_session": False,
        "first_session_started": Event(),
        "release_first_session": Event(),
        "hold_import": False,
        "import_started": Event(),
        "release_import": Event(),
        "signed_out": False,
        "interactive_connections": [],
        "interactive_state": "verified",
        "disconnect_after_frame": False,
        "compressed_frame": False,
        "finishes": [],
        "finish_outcome": {"verified": {}},
        "session_failures_remaining": 0,
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

    @app.get("/api/instances")
    def instances() -> dict[str, Any]:
        state["metadata_requests"] += 1
        if state["metadata_failures_remaining"]:
            state["metadata_failures_remaining"] -= 1
            raise HTTPException(status_code=503, detail="temporarily unavailable")
        result = []
        for instance, provider in state["instances"]:
            item = {"ref": {"instanceId": instance, "providerId": provider}}
            info = state["instance_info"].get(
                instance,
                {"instructions": state["instructions"], "loginUrl": f"https://{instance}.example.test/sign-in"},
            )
            if info is not None:
                item["info"] = info
            result.append(item)
        return {"instances": result}

    @app.get("/api/instances/{instance_id}/session")
    async def session(instance_id: str) -> dict[str, Any]:
        request_count = state["session_requests"].get(instance_id, 0) + 1
        state["session_requests"][instance_id] = request_count
        if instance_id == "one" and state["session_failures_remaining"]:
            state["session_failures_remaining"] -= 1
            raise HTTPException(status_code=503, detail="temporarily unavailable")
        if state["delay_first_session"] and instance_id == "one" and request_count == 1:
            state["first_session_started"].set()
            await state["release_first_session"].wait()
        interactive_unverified = instance_id == "one" and state["interactive_state"] == "unverified"
        message = (
            "Sign-in required"
            if interactive_unverified
            else "Fresh status"
            if request_count > 1 and instance_id == "one"
            else "Connected"
        )
        return {
            "snapshot": {
                "known": {
                    "state": "SESSION_STATE_UNVERIFIED" if interactive_unverified else "SESSION_STATE_VERIFIED",
                    "stateChangedAt": "2026-11-01T00:00:00Z",
                },
                "ownerPresentation": {"message": message},
                "importedAt": "2026-11-01T00:00:00Z",
                "lastProbe": {"verified": {}, "source": "PROBE_SOURCE_MANUAL", "completedAt": "2026-11-01T01:00:00Z"},
            }
        }

    @app.post("/api/instances/{instance_id}:probe")
    def probe(instance_id: str) -> dict[str, Any]:
        state["probes"].append(instance_id)
        return {"result": state["probe_outcome"]}

    @app.post("/api/instances/{instance_id}:importSession")
    async def import_session(instance_id: str, request: Request) -> JSONResponse:
        body = await request.json()
        candidate = json.loads(base64.b64decode(body["candidate"], validate=True))
        state["imports"].append((instance_id, candidate))
        if state["hold_import"]:
            state["import_started"].set()
            await state["release_import"].wait()
        response_case = candidate["session"].get("response_case")
        responses: dict[str, dict[str, Any]] = {
            "challenge": {"challengeRequired": {}},
            "unknown": {"outcomeUnknown": {}},
            "unknown_invalid": {"outcomeUnknown": {"reason": "UNKNOWN_OUTCOME_REASON_INVALID_RESPONSE"}},
            "unknown_http": {
                "outcomeUnknown": {"reason": "UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR", "upstreamHttpStatus": 429}
            },
            "unknown_http_invalid": {
                "outcomeUnknown": {"reason": "UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR", "upstreamHttpStatus": 999}
            },
            "timeout": {"outcomeUnknown": {"reason": "UNKNOWN_OUTCOME_REASON_TIMEOUT"}},
            "rejected": {"rejected": {"reason": "CANDIDATE_REJECTION_REASON_MALFORMED"}},
            "reauth": {"reauthenticationRequired": {}},
            "unavailable": {"upstreamUnavailable": {}},
            "invalid_response": {"failure": {"reason": "IMPORT_FAILURE_REASON_INVALID_RESPONSE"}},
            "storage_error": {"failure": {"reason": "IMPORT_FAILURE_REASON_STORAGE_ERROR"}},
            "upstream_http": {
                "failure": {"reason": "IMPORT_FAILURE_REASON_UPSTREAM_HTTP_ERROR", "upstreamHttpStatus": 429}
            },
            "future": {"outcomeFromFuture": {}, "message": "must not be displayed"},
        }
        if response_case == "lost_response":
            return JSONResponse({}, status_code=503)
        result = responses.get(response_case, {"installed": {}})
        result["message"] = "provider-internal response with private data"
        return JSONResponse(result)

    @app.websocket("/api/instances/{instance_id}/interactive-sign-in/ws")
    async def interactive_sign_in(websocket: WebSocket, instance_id: str) -> None:
        await websocket.accept(subprotocol="binary" if "binary" in websocket.scope.get("subprotocols", []) else None)
        state["interactive_connections"].append(instance_id)
        if instance_id == "one":
            state["interactive_state"] = "unverified"
        try:
            await websocket.send_bytes(b"RFB 003.008\n")
            assert (await websocket.receive_bytes()).startswith(b"RFB 003.")
            await websocket.send_bytes(b"\x01\x01")  # one security type: None
            assert await websocket.receive_bytes() == b"\x01"
            await websocket.send_bytes(b"\x00\x00\x00\x00")
            assert await websocket.receive_bytes() in (b"\x00", b"\x01")
            pixel_format = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
            await websocket.send_bytes(struct.pack(">HH", 640, 480) + pixel_format + struct.pack(">I", 4) + b"Demo")

            # A deterministic raw framebuffer proves that the noVNC canvas
            # displays remote pixels, rather than just completing its handshake.
            def rectangle(x: int, y: int, width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
                red, green, blue = rgb
                return struct.pack(">HHHHi", x, y, width, height, 0) + bytes((red, green, blue, 0)) * width * height

            framebuffer = b"\x00\x00" + struct.pack(">H", 6 if state["compressed_frame"] else 4)
            framebuffer += rectangle(0, 0, 640, 480, (241, 245, 249))
            framebuffer += rectangle(0, 0, 640, 64, (29, 78, 216))
            framebuffer += rectangle(140, 116, 360, 268, (255, 255, 255))
            framebuffer += rectangle(230, 304, 180, 42, (37, 99, 235))
            if state["compressed_frame"]:
                # Tight JPEG and TightPNG use noVNC's Image/data-URL render path.
                # Raw rectangles above cannot catch a CSP that blocks these images.
                def tight_image(
                    x: int, encoding: int, control: int, rgb: tuple[int, int, int], image_format: str
                ) -> bytes:
                    image = BytesIO()
                    Image.new("RGB", (64, 64), rgb).save(image, format=image_format)
                    data = image.getvalue()
                    length = len(data)
                    assert length < 16384
                    compact_length = bytes(((length & 0x7F) | 0x80, length >> 7))
                    return struct.pack(">HHHHi", x, 120, 64, 64, encoding) + bytes((control,)) + compact_length + data

                framebuffer += tight_image(24, 7, 0x90, (200, 20, 30), "JPEG")
                framebuffer += tight_image(552, -260, 0xA0, (20, 180, 80), "PNG")
            await websocket.send_bytes(framebuffer)
            if state["disconnect_after_frame"]:
                await websocket.close()
                return
            while True:
                await websocket.receive_bytes()
        except WebSocketDisconnect:
            pass

    @app.post("/api/instances/{instance_id}:finishInteractiveSignIn")
    def finish_interactive_sign_in(instance_id: str) -> dict[str, Any]:
        state["finishes"].append(instance_id)
        if instance_id == "one" and "verified" in state["finish_outcome"]:
            state["interactive_state"] = "verified"
        return {"result": state["finish_outcome"]}

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


async def test_provider_sign_in_links(browser: Browser, dashboard: tuple[str, dict[str, Any]]) -> None:
    origin, _ = dashboard
    async with await browser.new_context() as context:
        await context.route("https://*.example.test/**", lambda route: route.fulfill(body="Provider sign-in"))
        page = await context.new_page()
        await page.goto(origin)
        for instance in ("one", "two", "beta"):
            card = page.get_by_role("region", name=f"{instance} integration")
            link = card.get_by_role("link", name=f"Sign in to {instance}", exact=True)
            await expect(link).to_be_visible()
            await expect(card.get_by_text("Copy the exported session.")).not_to_be_visible()
            await expect(link).to_have_attribute("href", f"https://{instance}.example.test/sign-in")
            await expect(link).to_have_attribute("target", "_blank")
            await expect(link).to_have_attribute("rel", "noopener noreferrer")
            async with context.expect_page() as opened:
                await link.click()
            destination = await opened.value
            await destination.wait_for_url(f"https://{instance}.example.test/sign-in")
            assert await destination.evaluate("window.opener === null")
            assert await destination.evaluate("document.referrer") == ""
            assert page.url == origin + "/"
            await destination.close()


async def test_interactive_sign_in_requires_non_null_capability(
    browser: Browser, dashboard: tuple[str, dict[str, Any]]
) -> None:
    origin, state = dashboard
    # The gateway's protojson response emits null for an unset message field.
    state["instance_info"] = {
        "one": {"interactiveSignIn": None, "loginUrl": "https://one.example.test/sign-in"},
        "two": {"interactiveSignIn": {}, "loginUrl": "https://two.example.test/sign-in"},
        "beta": {"interactiveSignIn": "invalid", "loginUrl": "https://beta.example.test/sign-in"},
    }
    async with await browser.new_context() as context:
        page = await context.new_page()
        await page.goto(origin)
        response = await page.request.get(f"{origin}/api/instances")
        payload = await response.json()
        assert [item["info"]["interactiveSignIn"] for item in payload["instances"]] == [None, {}, "invalid"]
        for instance in ("one", "beta"):
            card = page.get_by_role("region", name=f"{instance} integration")
            await expect(card.get_by_role("button", name="Open sign-in browser")).to_have_count(0)
            await expect(card.get_by_role("link", name=f"Sign in to {instance}")).to_have_text("Sign in")
        supported = page.get_by_role("region", name="two integration")
        await expect(supported.get_by_role("button", name="Open sign-in browser")).to_be_visible()


async def test_interactive_sign_in_finishes_and_closes_viewer(
    browser: Browser, dashboard: tuple[str, dict[str, Any]]
) -> None:
    origin, state = dashboard
    state["instance_info"] = {
        "one": {"interactiveSignIn": {}, "instructions": [], "loginUrl": "https://one.example.test/sign-in"}
    }
    context = await browser.new_context()
    page = await context.new_page()
    errors: list[str] = []
    _listen_for_browser_errors(page, errors)
    await page.goto(origin, wait_until="domcontentloaded")
    card = page.get_by_role("region", name="one integration")
    await expect(card.get_by_role("button", name="Open sign-in browser")).to_be_visible()
    await expect(card.get_by_text("Connected")).to_be_visible()
    await expect(card.get_by_role("link", name="Sign in to one")).to_have_text("Open provider site in a new tab")
    await expect(card.get_by_role("button", name="Export instructions")).to_have_count(0)
    await card.get_by_role("button", name="Open sign-in browser").click()
    modal = page.get_by_role("dialog", name="Sign in to one")
    await expect(modal.get_by_role("application", name="one remote display")).to_be_visible()
    await expect(modal.get_by_text("Remote browser connected.")).to_be_visible()
    await _wait_for_remote_pixels(page)
    assert state["interactive_connections"] == ["one"]
    assert state["interactive_state"] == "unverified"
    await modal.get_by_role("button", name="Finish sign-in").click()
    await expect(card.get_by_role("button", name="Open sign-in browser")).to_be_visible()
    await expect(modal).to_have_count(0)
    assert state["finishes"] == ["one"]
    assert state["interactive_state"] == "verified"
    await expect(card.get_by_text("Fresh status")).to_be_visible()
    assert errors == []
    await context.close()


async def test_interactive_sign_in_renders_compressed_images(
    browser: Browser, dashboard: tuple[str, dict[str, Any]]
) -> None:
    origin, state = dashboard
    state["instance_info"] = {"one": {"interactiveSignIn": {}, "instructions": []}}
    state["compressed_frame"] = True
    context = await browser.new_context()
    page = await context.new_page()
    errors: list[str] = []
    _listen_for_browser_errors(page, errors)
    response = await page.goto(origin, wait_until="domcontentloaded")
    assert response is not None
    assert response.headers.get("content-security-policy") == _CSP
    await page.get_by_role("region", name="one integration").get_by_role("button", name="Open sign-in browser").click()
    await _wait_for_remote_pixels(page)
    await _wait_for_compressed_pixels(page)
    assert errors == []
    await context.close()


@pytest.mark.parametrize("status_fails", [False, True])
async def test_closing_interactive_viewer_refreshes_status(
    browser: Browser, dashboard: tuple[str, dict[str, Any]], status_fails: bool
) -> None:
    origin, state = dashboard
    state["instance_info"] = {"one": {"interactiveSignIn": {}, "instructions": []}}
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(origin, wait_until="domcontentloaded")
    card = page.get_by_role("region", name="one integration")
    await expect(card.get_by_text("Connected")).to_be_visible()
    await card.get_by_role("button", name="Open sign-in browser").click()
    modal = page.get_by_role("dialog", name="Sign in to one")
    await expect(modal.get_by_text("Remote browser connected.")).to_be_visible()
    assert state["interactive_state"] == "unverified"
    if status_fails:
        state["session_failures_remaining"] = 1
    await modal.get_by_role("button", name="Close browser").click()
    await expect(modal).to_have_count(0)
    await expect(card.get_by_text("Status unavailable" if status_fails else "Sign-in required")).to_be_visible()
    await expect(card.get_by_text("Connected")).to_have_count(0)
    assert state["finishes"] == []
    await context.close()


async def test_lost_interactive_viewer_refreshes_status(
    browser: Browser, dashboard: tuple[str, dict[str, Any]]
) -> None:
    origin, state = dashboard
    state["instance_info"] = {"one": {"interactiveSignIn": {}, "instructions": []}}
    state["disconnect_after_frame"] = True
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(origin, wait_until="domcontentloaded")
    card = page.get_by_role("region", name="one integration")
    await expect(card.get_by_text("Connected")).to_be_visible()
    await card.get_by_role("button", name="Open sign-in browser").click()
    modal = page.get_by_role("dialog", name="Sign in to one")
    await expect(modal.get_by_text("Browser connection ended.", exact=False)).to_be_visible()
    await expect(card.get_by_text("Sign-in required")).to_be_visible()
    await expect(modal.get_by_role("button", name="Reconnect")).to_be_visible()
    assert state["finishes"] == []
    await context.close()


async def test_failed_interactive_finish_keeps_browser_open(
    browser: Browser, dashboard: tuple[str, dict[str, Any]]
) -> None:
    origin, state = dashboard
    state["instance_info"] = {"one": {"interactiveSignIn": {}, "instructions": []}}
    state["finish_outcome"] = {"challengeRequired": {}}
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(origin, wait_until="domcontentloaded")
    card = page.get_by_role("region", name="one integration")
    await card.get_by_role("button", name="Open sign-in browser").click()
    modal = page.get_by_role("dialog", name="Sign in to one")
    await expect(modal.get_by_text("Remote browser connected.")).to_be_visible()
    await modal.get_by_role("button", name="Finish sign-in").click()
    await expect(modal.get_by_text("requires a security challenge", exact=False)).to_be_visible()
    await expect(modal.get_by_role("application", name="one remote display")).to_be_visible()
    assert state["finishes"] == ["one"]
    await context.close()


async def test_invalid_sign_in_urls_preserve_cards(browser: Browser, dashboard: tuple[str, dict[str, Any]]) -> None:
    origin, state = dashboard
    invalid_urls = [
        None,
        "",
        42,
        "javascript:alert(1)",
        "data:text/html,signin",
        "http://signin.example.test/",
        "//signin.example.test/",
        "/sign-in",
        "https://",
        "https://?signin",
        "https://user:secret@signin.example.test/",
        "https://@signin.example.test/",
        "https://signin.example.test/\n",
        "https://signin.example.test/\x00",
        "https://signin.example.test\\@other.example.test/",
        "https://signin.example.test:bad/",
        "https://127.1/sign-in",
    ]
    state["instance_info"] = {
        f"unsafe-{index}": {"loginUrl": value, "instructions": state["instructions"]}
        for index, value in enumerate(invalid_urls)
    } | {"missing-url": {}, "unavailable-info": None}
    state["instances"] = [(instance, "alpha") for instance in state["instance_info"]]
    async with await browser.new_context() as context:
        page = await context.new_page()
        await page.goto(origin)
        for instance in state["instance_info"]:
            card = page.get_by_role("region", name=f"{instance} integration")
            await expect(card).to_be_visible()
            await expect(card.get_by_role("link")).to_have_count(0)
            await expect(card.get_by_role("button", name="Check again")).to_be_enabled()
        card = page.get_by_role("region", name="unsafe-0 integration")
        await card.get_by_role("button", name="Export instructions").click()
        await expect(card.get_by_text("Copy the exported session.")).to_be_visible()


@pytest.mark.parametrize("clipboard_fails", [False, True])
async def test_export_command_copy(
    browser: Browser, dashboard: tuple[str, dict[str, Any]], clipboard_fails: bool
) -> None:
    origin, state = dashboard
    command = "chrome-export --profile 'Profile 2' --format=json\n# export only"
    state["instructions"] = [{"text": "Copy the exported session.", "command": command}]
    async with await browser.new_context() as context:
        page = await context.new_page()
        await page.add_init_script(
            """window.copiedCommands = [];
            Object.defineProperty(navigator, 'clipboard', {value: {writeText: async command => {
                window.copiedCommands.push(command);
                if (CLIPBOARD_FAILS) throw new Error('Clipboard denied');
            }}});""".replace("CLIPBOARD_FAILS", json.dumps(clipboard_fails))
        )
        await page.goto(origin)
        card = page.get_by_role("region", name="one integration")
        await card.get_by_role("button", name="Export instructions").click()
        await expect(card.locator("pre")).to_have_text(command)
        await card.get_by_role("button", name="Copy export command", exact=True).click()
        assert await page.evaluate("window.copiedCommands") == [command]
        if clipboard_fails:
            await expect(card.get_by_role("alert")).to_contain_text("Could not copy")
            await expect(card.get_by_role("status")).to_have_count(0)
            await expect(card.get_by_role("button", name="Copy export command")).to_have_text("Copy")
        else:
            await expect(card.get_by_role("status")).to_have_text("Command copied.")
            await expect(card.get_by_role("button", name="Copy export command")).to_have_text("Copied")
            await expect(card.get_by_role("button", name="Copy export command")).to_have_text("Copy")
        await expect(card.locator("pre")).to_have_text(command)
        assert state["imports"] == []


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

    export = json.dumps({"provider": "alpha", "session": {"access_token": "secret-session-token"}})
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
        body = json.dumps({"provider": "beta", "session": {"response_case": response_case}})
        await session_input.fill(body)
        async with page.expect_response(lambda response: response.url.endswith("/beta:importSession")):
            await page.get_by_role("button", name="Verify and import").click()
        await expect(page.get_by_role("status")).to_contain_text(re.compile(expected))
        await expect(page.get_by_role("status")).not_to_contain_text("provider-internal")
        assert await session_input.input_value() == ""

    disabled_provider = json.dumps({"provider": "not-enabled", "session": {"cookie": "private"}})
    previous_imports = len(state["imports"])
    await session_input.fill(disabled_provider)
    await expect(page.get_by_text("not-enabled is not enabled on this service.")).to_be_visible()
    await page.get_by_role("button", name="Verify and import").click()
    await expect(page.get_by_role("status")).to_contain_text("The session was not sent")
    assert len(state["imports"]) == previous_imports
    assert errors == [], f"browser errors during provider/import flow: {errors}"
    assert await page.locator("style").count() == 0
    await context.close()


async def test_provider_instructions_refresh_without_reload(
    browser: Browser, dashboard: tuple[str, dict[str, Any]]
) -> None:
    origin, state = dashboard
    context = await browser.new_context(color_scheme="light")
    page = await context.new_page()
    await page.clock.install()
    page_loads: list[str] = []
    page.on("load", lambda _: page_loads.append(page.url))
    await page.goto(origin, wait_until="domcontentloaded")
    await page.wait_for_load_state("load")

    card = page.get_by_role("region", name="one integration")
    await expect(card.get_by_role("button", name="Export instructions")).to_be_visible()
    await card.get_by_role("button", name="Export instructions").click()
    await expect(card.get_by_text("Copy the exported session.")).to_be_visible()
    assert state["metadata_requests"] == 1
    initial_page_loads = len(page_loads)

    state["instructions"] = [{"text": "Updated export steps from the provider."}]
    async with page.expect_response(lambda response: response.url.endswith("/api/instances")):
        await page.clock.fast_forward(30_000)
    await expect(card.get_by_text("Updated export steps from the provider.")).to_be_visible()
    await expect(card.get_by_text("Copy the exported session.")).to_have_count(0)
    assert state["metadata_requests"] == 2
    assert len(page_loads) == initial_page_loads
    await context.close()


async def test_initial_metadata_failure_retries_without_page_reload(
    browser: Browser, dashboard: tuple[str, dict[str, Any]]
) -> None:
    origin, state = dashboard
    state["metadata_failures_remaining"] = 1
    context = await browser.new_context(color_scheme="light")
    page = await context.new_page()
    await page.clock.install()
    page_loads: list[str] = []
    page.on("load", lambda _: page_loads.append(page.url))
    await page.goto(origin, wait_until="domcontentloaded")

    await expect(page.get_by_role("status")).to_contain_text(
        "Could not load integrations. We'll retry automatically in 30 seconds."
    )
    assert state["metadata_requests"] == 1
    initial_page_loads = len(page_loads)
    async with page.expect_response(lambda response: response.url.endswith("/api/instances")):
        await page.clock.fast_forward(30_000)
    await expect(page.get_by_role("region", name="one integration")).to_be_visible()
    assert state["metadata_requests"] == 2
    assert len(page_loads) == initial_page_loads
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
    export = json.dumps({"provider": "beta", "session": {"cookie": "secret-cookie"}})
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


async def test_manual_probe_shows_its_own_unknown_result(
    browser: Browser, dashboard: tuple[str, dict[str, Any]]
) -> None:
    origin, state = dashboard
    state["probe_outcome"] = {"outcomeUnknown": {"credentialRotationMayHaveOccurred": True}}
    context = await browser.new_context()
    page = await context.new_page()
    await page.clock.install()
    await page.goto(origin, wait_until="domcontentloaded")
    card = page.get_by_role("region", name="one integration")
    await expect(card.get_by_text("Connected")).to_be_visible()
    await card.get_by_role("button", name="Check again").click()
    await expect(card.get_by_text("Last check: outcome unknown; credentials may have changed.")).to_be_visible()
    await expect(card.get_by_role("row").filter(has_text="Last check result")).to_contain_text("unknown")
    await expect(card.get_by_role("row").filter(has_text="Last check result")).not_to_contain_text("success")
    await page.clock.fast_forward(30_000)
    await expect(card.get_by_text("Check outcome unknown")).to_be_visible()
    await expect(card.get_by_role("row").filter(has_text="Last check result")).to_contain_text("unknown")
    await context.close()


async def test_manual_probe_badge_reflects_the_returned_challenge(
    browser: Browser, dashboard: tuple[str, dict[str, Any]]
) -> None:
    origin, state = dashboard
    state["probe_outcome"] = {"challengeRequired": {}}
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(origin, wait_until="domcontentloaded")
    card = page.get_by_role("region", name="one integration")
    await expect(card.get_by_text("Connected")).to_be_visible()
    await card.get_by_role("button", name="Check again").click()
    await expect(card.get_by_text("Provider security challenge")).to_be_visible()
    await expect(card.get_by_role("row").filter(has_text="Last check result")).to_contain_text("challenge required")
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


async def test_lost_import_response_requires_fresh_export_and_reload_does_not_replay(
    browser: Browser, dashboard: tuple[str, dict[str, Any]]
) -> None:
    origin, state = dashboard
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(origin, wait_until="domcontentloaded")
    await expect(page.get_by_text("Connected").first).to_be_visible()
    await page.get_by_label("Session export").fill(
        json.dumps({"provider": "beta", "session": {"cookie": "secret-cookie", "response_case": "lost_response"}})
    )
    await page.get_by_role("button", name="Verify and import").click()
    await expect(page.get_by_role("status")).to_contain_text("Obtain a fresh export")
    await expect(page.get_by_label("Session export")).to_have_value("")
    assert len(state["imports"]) == 1
    await page.reload(wait_until="domcontentloaded")
    await expect(page.get_by_text("Connected").first).to_be_visible()
    await expect(page.get_by_label("Session export")).to_have_value("")
    assert len(state["imports"]) == 1
    await context.close()


async def test_disconnect_does_not_replay_import_on_reload(
    browser: Browser, dashboard: tuple[str, dict[str, Any]]
) -> None:
    origin, state = dashboard
    state["hold_import"] = True
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(origin, wait_until="domcontentloaded")
    await expect(page.get_by_text("Connected").first).to_be_visible()
    await page.get_by_label("Session export").fill(
        json.dumps({"provider": "beta", "session": {"cookie": "secret-cookie"}})
    )
    await page.get_by_role("button", name="Verify and import").click()
    await wait_for(state["import_started"].wait(), timeout=10)
    await page.reload(wait_until="domcontentloaded")
    await expect(page.get_by_text("Connected").first).to_be_visible()
    state["release_import"].set()
    await expect(page.get_by_label("Session export")).to_have_value("")
    assert len(state["imports"]) == 1
    await context.close()


@pytest.mark.parametrize(("scheme", "width"), [("light", 1200), ("dark", 390)])
async def test_dashboard_visual(
    playwright: Playwright,
    test_outputs: BoundTestOutputs,
    dashboard: tuple[str, dict[str, Any]],
    scheme: Literal["light", "dark"],
    width: int,
) -> None:
    origin, state = dashboard
    command = "bb run --config=rbe //platform_sessions:export -- alpha-example chrome"
    state["instructions"] = [{"text": "Copy the exported session.", "command": command}]
    async with (
        deterministic_browser_context(
            playwright, viewport={"width": width, "height": 900}, frozen_now_ms=None, color_scheme=scheme
        ) as context,
        open_visual_page(context, test_outputs, title="Platform sessions") as view,
    ):
        await view.page.goto(origin, wait_until="domcontentloaded")
        await expect(view.page.get_by_text("Connected").first).to_be_visible()
        await (
            view.page.get_by_role("region", name="one integration")
            .get_by_role("button", name="Export instructions")
            .click()
        )
        await expect(view.page.get_by_role("button", name="Copy export command")).to_be_in_viewport()
        code = view.page.get_by_role("region", name="one integration").locator("pre")
        await expect(code).to_have_text(command)
        assert await code.evaluate(
            "element => element.scrollWidth <= element.clientWidth || "
            "['auto', 'scroll'].includes(getComputedStyle(element).overflowX)"
        )
        assert await view.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        await view.capture(full_page=True, animations="disabled")


async def test_unknown_manual_probe_visual(
    playwright: Playwright, test_outputs: BoundTestOutputs, dashboard: tuple[str, dict[str, Any]]
) -> None:
    origin, state = dashboard
    state["probe_outcome"] = {"outcomeUnknown": {"credentialRotationMayHaveOccurred": True}}
    async with (
        deterministic_browser_context(
            playwright, viewport={"width": 390, "height": 900}, frozen_now_ms=None, color_scheme="dark"
        ) as context,
        open_visual_page(context, test_outputs, title="Unknown manual check") as view,
    ):
        await view.page.goto(origin, wait_until="domcontentloaded")
        card = view.page.get_by_role("region", name="one integration")
        await expect(card.get_by_text("Connected")).to_be_visible()
        await card.get_by_role("button", name="Check again").click()
        await expect(card.get_by_text("Last check: outcome unknown; credentials may have changed.")).to_be_visible()
        await view.capture(full_page=True, animations="disabled")


async def test_unknown_import_visual(
    playwright: Playwright, test_outputs: BoundTestOutputs, dashboard: tuple[str, dict[str, Any]]
) -> None:
    origin, state = dashboard
    del state
    async with (
        deterministic_browser_context(
            playwright, viewport={"width": 390, "height": 900}, frozen_now_ms=None, color_scheme="dark"
        ) as context,
        open_visual_page(context, test_outputs, title="Unknown import outcome") as view,
    ):
        await view.page.goto(origin, wait_until="domcontentloaded")
        await expect(view.page.get_by_text("Connected").first).to_be_visible()
        await view.page.get_by_label("Session export").fill(
            json.dumps({"provider": "beta", "session": {"response_case": "unknown"}})
        )
        await view.page.get_by_role("button", name="Verify and import").click()
        await expect(view.page.get_by_role("status")).to_contain_text("Obtain a fresh export")
        await view.capture(full_page=True, animations="disabled")


async def test_interactive_sign_in_visual(
    playwright: Playwright, test_outputs: BoundTestOutputs, dashboard: tuple[str, dict[str, Any]]
) -> None:
    origin, state = dashboard
    state["instance_info"] = {"one": {"interactiveSignIn": {}, "instructions": []}}
    state["compressed_frame"] = True
    async with (
        deterministic_browser_context(
            playwright, viewport={"width": 1200, "height": 900}, frozen_now_ms=None, color_scheme="light"
        ) as context,
        open_visual_page(context, test_outputs, title="Interactive sign-in browser") as view,
    ):
        await view.page.goto(origin, wait_until="domcontentloaded")
        card = view.page.get_by_role("region", name="one integration")
        await card.get_by_role("button", name="Open sign-in browser").click()
        modal = view.page.get_by_role("dialog", name="Sign in to one")
        await expect(modal.get_by_text("Remote browser connected.")).to_be_visible()
        await expect(modal.get_by_role("application", name="one remote display")).to_be_visible()
        await _wait_for_remote_pixels(view.page)
        await _wait_for_compressed_pixels(view.page)
        await view.capture("desktop", animations="disabled")
        await view.page.set_viewport_size({"width": 390, "height": 900})
        await _wait_for_remote_pixels(view.page)
        await _wait_for_compressed_pixels(view.page)
        await expect(modal.get_by_role("button", name="Finish sign-in")).to_be_in_viewport()
        await view.capture("mobile", animations="disabled")
        await modal.get_by_role("button", name="Close browser").click()
        await expect(modal).to_have_count(0)
        await expect(card.get_by_text("Sign-in required")).to_be_visible()
        await view.capture("after-close", full_page=True, animations="disabled")


if __name__ == "__main__":
    pytest_bazel.main()
