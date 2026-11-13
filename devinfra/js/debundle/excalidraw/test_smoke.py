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
        redo = page.get_by_test_id("button-redo")
        await expect(undo).to_be_disabled()
        await expect(redo).to_be_disabled()

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
        await undo.click()
        await expect(undo).to_be_disabled()
        await expect(redo).to_be_enabled()
        await redo.click()
        await expect(undo).to_be_enabled()
        await expect(redo).to_be_disabled()

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


async def test_scene_class(playwright_driver: Playwright, origin: str) -> None:
    async with deterministic_browser_context(
        playwright_driver, viewport={"width": 1280, "height": 800}, frozen_now_ms=None
    ) as context:
        page = await context.new_page()
        await page.goto(origin)
        await page.wait_for_function("document.documentElement.dataset.debundleHarnessLoaded === 'true'")
        result = await page.evaluate(
            """async () => {
                const { Scene } = await import('./editor/packages/excalidraw/scene/Scene.js');
                const scene = new Scene();
                let updates = 0;
                const detach = scene.onUpdate(() => updates++);
                const visible = {id: 'scene-visible', type: 'rectangle', index: 'a0',
                                 isDeleted: false, groupIds: [], boundElements: null, frameId: null};
                const deleted = {...visible, id: 'scene-deleted', index: 'a1', isDeleted: true};
                scene.replaceAllElements([visible, deleted]);
                const beforeDestroy = {
                    all: scene.getElementsIncludingDeleted().map(element => element.id),
                    visible: scene.getNonDeletedElements().map(element => element.id),
                    selected: scene.getSelectedElements({selectedElementIds: {'scene-visible': true}})
                                   .map(element => element.id),
                    lookup: [Scene.getScene(visible) === scene, Scene.getScene(visible.id) === scene,
                             scene.getElement(deleted.id) === deleted,
                             scene.getNonDeletedElement(deleted.id)],
                    unchanged: scene.mapElements(element => element),
                    updates,
                };
                detach();
                scene.triggerUpdate();
                scene.destroy();
                return {beforeDestroy, updates, afterDestroy: [Scene.getScene(visible.id),
                                                               scene.getElementsIncludingDeleted().length]};
            }"""
        )
        assert result == {
            "beforeDestroy": {
                "all": ["scene-visible", "scene-deleted"],
                "visible": ["scene-visible"],
                "selected": ["scene-visible"],
                "lookup": [True, True, True, None],
                "unchanged": False,
                "updates": 1,
            },
            "updates": 1,
            "afterDestroy": [None, 0],
        }


async def test_store_and_changes(playwright_driver: Playwright, origin: str) -> None:
    async with deterministic_browser_context(
        playwright_driver, viewport={"width": 1280, "height": 800}, frozen_now_ms=None
    ) as context:
        page = await context.new_page()
        await page.goto(origin)
        await page.wait_for_function("document.documentElement.dataset.debundleHarnessLoaded === 'true'")
        result = await page.evaluate(
            """async () => {
                const { Store, getObservedAppState } = await import('./editor/packages/excalidraw/store.js');
                const { AppStateChange, ElementsChange } = await import('./editor/packages/excalidraw/change.js');
                const { getDefaultAppState } = await import('./editor/packages/excalidraw/appState.js');
                const before = {...getDefaultAppState(), name: 'before'};
                const after = {...before, name: 'after'};
                const observed = getObservedAppState(before);
                const appChange = AppStateChange.calculate(observed, getObservedAppState(after));
                const [applied] = appChange.applyTo(before, new Map());
                const [reversed] = appChange.inverse().applyTo(applied, new Map());

                const store = new Store();
                const events = [];
                const detach = store.onStoreIncrementEmitter.on(event => events.push([
                    event.elementsChange.isEmpty(), event.appStateChange.isEmpty()
                ]));
                store.shouldUpdateSnapshot();
                store.commit(new Map(), before);
                const originalSnapshot = store.snapshot;
                store.shouldCaptureIncrement();
                store.commit(undefined, {...before, width: before.width + 1});
                const ignoredUnobservedChange = store.snapshot === originalSnapshot;
                store.shouldCaptureIncrement();
                store.commit(undefined, after);
                const capturedName = store.snapshot.appState.name;
                store.commit(undefined, before);
                const ignoredUnscheduledChange = store.snapshot.appState.name === capturedName;
                store.shouldCaptureIncrement();
                store.commit(undefined, after);
                detach();
                store.clear();

                const rectangle = {
                    id: 'store-rectangle', type: 'rectangle', index: 'a0', x: 0, y: 0,
                    width: 10, height: 10, angle: 0, isDeleted: false, groupIds: [],
                    boundElements: null, frameId: null, version: 1, versionNonce: 1,
                    seed: 1, updated: 1, locked: false, link: null
                };
                const moved = {...rectangle, x: 25, version: 2, versionNonce: 2};
                const previousElements = new Map([[rectangle.id, rectangle]]);
                const nextElements = new Map([[moved.id, moved]]);
                const elementChange = ElementsChange.calculate(previousElements, nextElements);
                const [appliedElements] = elementChange.applyTo(previousElements, nextElements);
                const [reversedElements] = elementChange.inverse().applyTo(appliedElements, previousElements);
                return {
                    appChange: [appChange.isEmpty(), applied.name, reversed.name],
                    marker: [Object.getOwnPropertyDescriptor(observed, '__observedAppState').enumerable,
                             JSON.stringify(observed).includes('__observedAppState')],
                    store: [ignoredUnobservedChange, capturedName, ignoredUnscheduledChange,
                            events, store.snapshot.isEmpty()],
                    elements: [elementChange.isEmpty(), appliedElements.get(rectangle.id).x,
                               reversedElements.get(rectangle.id).x],
                };
            }"""
        )
        assert result == {
            "appChange": [False, "after", "before"],
            "marker": [False, False],
            "store": [True, "after", True, [[True, False]], True],
            "elements": [False, 25, 0],
        }


async def test_linear_element_editor(playwright_driver: Playwright, origin: str) -> None:
    async with deterministic_browser_context(
        playwright_driver, viewport={"width": 1280, "height": 800}, frozen_now_ms=None
    ) as context:
        page = await context.new_page()
        await page.goto(origin)
        await page.wait_for_function("document.documentElement.dataset.debundleHarnessLoaded === 'true'")
        result = await page.evaluate(
            """async () => {
                const { LinearElementEditor } = await import('./editor/packages/excalidraw/element/linearElementEditor.js');
                const line = {id: 'linear-editor', type: 'line', x: 10, y: 20,
                              width: 20, height: 10, angle: 0, points: [[0, 0], [20, 10]]};
                const elements = new Map([[line.id, line]]);
                const editor = new LinearElementEditor(line);
                const rounded = points => points.map(point => point.map(Math.round));
                return {
                    initial: [editor.elementId, editor.selectedPointsIndices, editor.isDragging,
                              editor.pointerOffset, editor.startBindingElement, editor.endBindingElement],
                    handleSize: LinearElementEditor.POINT_HANDLE_SIZE,
                    lookup: [LinearElementEditor.getElement(line.id, elements) === line,
                             LinearElementEditor.getElement('missing', elements)],
                    global: LinearElementEditor.getPointsGlobalCoordinates(line, elements),
                    rotated: rounded(LinearElementEditor.getPointsGlobalCoordinates({...line, angle: Math.PI}, elements)),
                    normalized: LinearElementEditor.getNormalizedPoints({...line, points: [[5, -3], [25, 7]]}),
                    equality: [LinearElementEditor.arePointsEqual(null, null),
                               LinearElementEditor.arePointsEqual([0, 0], null),
                               LinearElementEditor.arePointsEqual([2, 3], [2, 3])],
                };
            }"""
        )
        assert result == {
            "initial": ["linear-editor", None, False, {"x": 0, "y": 0}, "keep", "keep"],
            "handleSize": 10,
            "lookup": [True, None],
            "global": [[10, 20], [30, 30]],
            "rotated": [[30, 30], [10, 20]],
            "normalized": {"points": [[0, 0], [20, 10]], "x": 15, "y": 17},
            "equality": [True, False, True],
        }


async def test_action_manager(playwright_driver: Playwright, origin: str) -> None:
    async with deterministic_browser_context(
        playwright_driver, viewport={"width": 1280, "height": 800}, frozen_now_ms=None
    ) as context:
        page = await context.new_page()
        await page.goto(origin)
        await page.wait_for_function("document.documentElement.dataset.debundleHarnessLoaded === 'true'")
        result = await page.evaluate(
            """async () => {
                const { ActionManager } = await import('./main/packages/excalidraw/actions/manager.js');
                const updates = [];
                const state = {viewModeEnabled: false};
                const app = {props: {UIOptions: {canvasActions: {}}}};
                const manager = new ActionManager(update => updates.push(update),
                    () => state, () => [{id: 'one'}], app);
                const action = {
                    name: 'test-action', trackEvent: false,
                    predicate: elements => elements.length === 1,
                    keyTest: event => event.key === 'x',
                    perform: (elements, appState, value) => ({value, count: elements.length})
                };
                manager.registerAll([action, {...action, name: 'async-action', keyTest: () => false,
                    perform: async (elements, appState, value) => ({value, count: elements.length})}]);
                manager.executeAction(action, 'api', 'direct');
                manager.executeAction(manager.actions['async-action'], 'api', 'async');
                await Promise.resolve();
                let consumed = 0;
                const key = {key: 'x', preventDefault: () => consumed++, stopPropagation: () => consumed++};
                const dispatched = manager.handleKeyDown(key);
                state.viewModeEnabled = true;
                const viewOnly = manager.handleKeyDown(key);
                state.viewModeEnabled = false;
                app.props.UIOptions.canvasActions['test-action'] = false;
                const disabled = manager.handleKeyDown(key);
                return {updates, consumed, dispatched, viewOnly, disabled,
                    enabled: [manager.isActionEnabled(action),
                              manager.isActionEnabled({...action, predicate: () => false})]};
            }"""
        )
        assert result == {
            "updates": [{"value": "direct", "count": 1}, {"value": "async", "count": 1}, {"value": None, "count": 1}],
            "consumed": 2,
            "dispatched": True,
            "viewOnly": False,
            "disabled": False,
            "enabled": [True, False],
        }


if __name__ == "__main__":
    pytest_bazel.main()
