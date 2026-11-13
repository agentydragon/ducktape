"""The accepted debundled Excalidraw app loads and responds in headless Chromium."""

# gazelle:include_dep //util/testing/frontend_visual:fixtures

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
import pytest_bazel
from playwright.async_api import ConsoleMessage, Error, Page, Playwright, Request, Response, expect
from python.runfiles import Runfiles
from starlette.applications import Starlette
from starlette.requests import Request as StarletteRequest  # Playwright also names a Request.
from starlette.responses import HTMLResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from util.testing.asgi import serve_app
from util.testing.frontend_visual.context import deterministic_browser_context

pytest_plugins = ("util.testing.frontend_visual.fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")

PACKAGES_PREFIX = "/__packages"


def resolve(runfiles: Runfiles, env_var: str) -> Path:
    return Path(str(runfiles.Rlocation(os.environ[env_var])))


def harness_app(runfiles: Runfiles) -> Starlette:
    """Serve the harness, with an import map for the packages vendor swaps import."""
    out = resolve(runfiles, "DEBUNDLE_OUT")
    package_roots = {
        name: Path(str(runfiles.Rlocation(path))) for name, path in json.loads(os.environ["PACKAGE_ROOTS"]).items()
    }
    swaps = json.loads((out / "reports/vendor_swaps.json").read_text())
    subpaths = {swap["package"]: swap["subpath"] for swap in swaps["full"].values()} | {
        package: coordinates["subpath"]
        for swap in swaps["partial"].values()
        for package, coordinates in swap["packages"].items()
    }
    import_map = {
        "imports": {package: f"{PACKAGES_PREFIX}/{package}/{subpath}" for package, subpath in subpaths.items()}
    }
    index_html = (out / "app/index.html").read_text()
    head = "<head>"
    page = index_html.replace(head, f'{head}\n<script type="importmap">{json.dumps(import_map)}</script>', 1)

    async def index(_: StarletteRequest) -> HTMLResponse:
        return HTMLResponse(page)

    return Starlette(
        routes=[
            Route("/", index),
            Route("/index.html", index),
            *(
                Mount(f"{PACKAGES_PREFIX}/{package}", app=StaticFiles(directory=package_roots[package]))
                for package in subpaths
            ),
            Mount("/fonts", app=StaticFiles(directory=resolve(runfiles, "EXCALIDRAW_PACKAGE") / "dist/prod/fonts")),
            Mount("/", app=StaticFiles(directory=out / "app")),
        ]
    )


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def origin(runfiles: Runfiles) -> AsyncIterator[str]:
    async with serve_app(harness_app(runfiles)) as served:
        yield served


async def test_app_loads_and_draws(playwright_driver: Playwright, origin: str) -> None:
    async with deterministic_browser_context(
        playwright_driver, viewport={"width": 1280, "height": 800}, frozen_now_ms=None
    ) as context:
        page: Page = await context.new_page()
        console_errors: list[str] = []
        failed_requests: list[str] = []

        def on_console(message: ConsoleMessage) -> None:
            if message.type == "error":
                console_errors.append(message.text)

        def on_response(response: Response) -> None:
            if response.status >= 400:
                failed_requests.append(f"{response.url} {response.status}")

        def on_request_failed(request: Request) -> None:
            failed_requests.append(f"{request.url} {request.failure}")

        def on_page_error(error: Error) -> None:
            console_errors.append(str(error))

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("requestfailed", on_request_failed)
        page.on("response", on_response)

        await page.goto(origin)
        await page.wait_for_function("document.documentElement.dataset.debundleHarnessLoaded === 'true'")
        await expect(page.locator(".App-toolbar")).to_be_visible()
        undo = page.get_by_test_id("button-undo")
        await expect(undo).to_be_disabled()

        # The tool is a visually hidden radio input; users click its label's icon.
        rectangle = page.get_by_test_id("toolbar-rectangle")
        await page.locator("label", has=rectangle).click()
        await expect(rectangle).to_be_checked()
        canvas = page.locator("canvas.excalidraw__canvas.interactive")
        await canvas.hover(position={"x": 400, "y": 300})
        await page.mouse.down()
        await page.mouse.move(600, 450, steps=5)
        await page.mouse.up()
        # Drawing records a history entry, which enables undo.
        await expect(undo).to_be_enabled()

        text_wrapping = await page.evaluate(
            """async () => {
                const { containsCJK, wrapText } = await import(
                    './editor/packages/excalidraw/element/textWrapping.js'
                );
                const context = document.createElement('canvas').getContext('2d');
                context.font = '20px monospace';
                return {
                    latin: wrapText('hello-world', context.font, context.measureText('hello-').width + 0.1),
                    emoji: wrapText('👨‍👩‍👧‍👦🇨🇿', context.font, 0),
                    composed: wrapText('c\\u030cて\\u3099', context.font, 0),
                    cjk: [containsCJK('hello'), containsCJK('hello界')],
                    wrapped: wrapText('ab cd', context.font, context.measureText('ab').width + 0.1),
                };
            }"""
        )
        assert text_wrapping == {
            "latin": "hello-\nworld",
            "emoji": "👨‍👩‍👧‍👦\n🇨🇿",
            "composed": "č\nで",
            "cjk": [False, True],
            "wrapped": "ab\ncd",
        }

        await page.wait_for_load_state("networkidle")
        assert await page.evaluate("globalThis.__debundleHarness.errors") == []
        assert console_errors == []
        assert failed_requests == []


async def test_element_type_checks(playwright_driver: Playwright, origin: str) -> None:
    async with deterministic_browser_context(
        playwright_driver, viewport={"width": 1280, "height": 800}, frozen_now_ms=None
    ) as context:
        page = await context.new_page()
        await page.goto(origin)
        await page.wait_for_function("document.documentElement.dataset.debundleHarnessLoaded === 'true'")
        result = await page.evaluate(
            """async () => {
                const checks = await import('./editor/packages/excalidraw/element/typeChecks.js');
                return {
                    images: [
                        checks.isImageElement(null),
                        checks.isImageElement({type: 'image'}),
                        checks.isInitializedImageElement({type: 'image'}),
                        checks.isInitializedImageElement({type: 'image', fileId: 'image-file'}),
                    ],
                    arrows: [
                        checks.isLinearElement({type: 'line'}),
                        checks.isBindingElement({type: 'line'}),
                        checks.isBindingElement({type: 'arrow', locked: true}),
                        checks.isBindingElement({type: 'arrow', locked: true}, false),
                        checks.isElbowArrow({type: 'arrow', elbowed: true}),
                    ],
                    containers: [
                        checks.isBindableElement({type: 'text', containerId: null}),
                        checks.isBindableElement({type: 'text', containerId: 'container'}),
                        checks.hasBoundTextElement({type: 'rectangle', boundElements: [{type: 'text'}]}),
                        checks.isBoundToContainer({type: 'text', containerId: 'container'}),
                        checks.isBoundToContainer({type: 'text', containerId: null}),
                    ],
                    frames: [
                        checks.isFrameElement({type: 'magicframe'}),
                        checks.isFrameLikeElement({type: 'magicframe'}),
                    ],
                    roundness: [
                        checks.getDefaultRoundnessTypeForElement({type: 'rectangle'}),
                        checks.getDefaultRoundnessTypeForElement({type: 'diamond'}),
                        checks.getDefaultRoundnessTypeForElement({type: 'text'}),
                    ],
                    fixedBindings: [checks.isFixedPointBinding({}), checks.isFixedPointBinding({fixedPoint: [0, 0]})],
                    elements: [checks.isExcalidrawElement({}), checks.isExcalidrawElement({type: 'selection'})],
                };
            }"""
        )
        assert result == {
            "images": [False, True, False, True],
            "arrows": [True, False, True, False, True],
            "containers": [True, False, True, True, False],
            "frames": [False, True],
            "roundness": [{"type": 3}, {"type": 2}, None],
            "fixedBindings": [False, True],
            "elements": [False, True],
        }


async def test_blob_and_file_helpers(playwright_driver: Playwright, origin: str) -> None:
    async with deterministic_browser_context(
        playwright_driver, viewport={"width": 1280, "height": 800}, frozen_now_ms=None
    ) as context:
        page = await context.new_page()
        await page.goto(origin)
        await page.wait_for_function("document.documentElement.dataset.debundleHarnessLoaded === 'true'")
        result = await page.evaluate(
            """async () => {
                const files = await import('./editor/packages/excalidraw/data/blob.js');
                const url = files.getDataURL_sync('hello 界', 'text/plain');
                const file = files.dataURLToFile(url, 'hello.txt');
                const png = await files.normalizeFile(new File([
                    new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])
                ], 'image.png', {type: 'image/jpeg'}));
                const libraryJSON = JSON.stringify({type: 'excalidrawlib', version: 2, libraryItems: []});
                const library = new File([libraryJSON], 'empty.excalidrawlib');
                const normalizedLibrary = await files.normalizeFile(library);
                const sceneOrLibrary = await files.loadSceneOrLibraryFromBlob(library, null, null);
                let invalidLibrary;
                try {
                    files.parseLibraryJSON('{}');
                } catch (error) {
                    invalidLibrary = error.message;
                }
                return {
                    roundTrip: [file.name, file.type, await file.text(), files.dataURLToString(url)],
                    asyncURL: await files.getDataURL(file),
                    bytes: [...new Uint8Array(await files.blobToArrayBuffer(new Blob(['abc'])))],
                    png: [png.name, png.type, png.size, files.isSupportedImageFile(png)],
                    library: [normalizedLibrary.type, await files.loadLibraryFromBlob(library),
                              sceneOrLibrary.type, files.parseLibraryJSON(libraryJSON)],
                    invalidLibrary,
                };
            }"""
        )
        assert result == {
            "roundTrip": ["hello.txt", "text/plain", "hello 界", "hello 界"],
            "asyncURL": "data:text/plain;base64,aGVsbG8g55WM",
            "bytes": [97, 98, 99],
            "png": ["image.png", "image/png", 8, True],
            "library": ["application/vnd.excalidrawlib+json", [], "application/vnd.excalidrawlib+json", []],
            "invalidLibrary": "Invalid library",
        }


if __name__ == "__main__":
    pytest_bazel.main()
