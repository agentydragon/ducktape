"""Run the original and certified splits of the pinned Excalidraw editor."""

# gazelle:include_dep //testing/frontend_visual:fixtures

import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path, PurePosixPath

import httpx
import pytest
import pytest_bazel
from playwright.async_api import ConsoleMessage, Error, Playwright, Request as BrowserRequest, Response, expect
from python.runfiles import Runfiles
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from testing.asgi import serve_app_in_loop
from testing.frontend_visual.context import deterministic_browser_context

pytest_plugins = ("testing.frontend_visual.fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")

SCROLL = "packages/excalidraw/scene/scroll.mjs"


def resolve(runfiles: Runfiles, path: str) -> Path:
    resolved = runfiles.Rlocation(path)
    assert resolved is not None, path
    return Path(resolved)


def bundled_file(runfiles: Runfiles, env_var: str, name: str) -> Path:
    (path,) = (path for path in os.environ[env_var].split() if path.endswith(f"/{name}"))
    return resolve(runfiles, path)


@pytest.fixture(scope="session")
def editor_source(runfiles: Runfiles) -> Path:
    bundle = bundled_file(runfiles, "EXCALIDRAW_OUTPUTS", "bundle")
    roster = bundled_file(runfiles, "EXCALIDRAW_INPUTS", "js-files.txt")
    (name,) = (line.split("\t")[1] for line in roster.read_text().splitlines() if line.startswith("editor.js\t"))
    return bundle / name


def certify_editor_split(runfiles: Runfiles, source: Path, origin: str) -> dict[str, Path]:
    canonical = json.loads(resolve(runfiles, os.environ["CANONICAL_PLACEMENT"]).read_text())
    resolved = resolve(runfiles, os.environ["SELECTED_PLACEMENT"])
    selected = json.loads(resolved.read_text())
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert selected["chunk_id"] == canonical["chunk_id"] == "editor"
    assert selected["source_sha256"] == canonical["source_sha256"] == digest

    for ordinal, (unit, full) in enumerate(zip(selected["units"], canonical["units"], strict=True)):
        assert unit["ordinal"] == full["ordinal"] == ordinal
        assert unit["owner"] is None or unit["owner"] == full["owner"], (ordinal, unit, full)
    files = [source.name, *(owner + ".mjs" for owner in selected["import_order"])]
    artifacts = undeclared_outputs_dir()
    (artifacts / "browser-origin.txt").write_text(f"{origin}\n")
    (artifacts / "placement.json").write_bytes(resolved.read_bytes())
    profile = artifacts / "host.json"
    profile.write_text(json.dumps({"profile": "browser-page", "stableIntrinsicValues": ["Math.PI@1"]}))
    out = artifacts / "certified-editor"
    command: list[str | Path] = [
        resolve(runfiles, os.environ["LOWERSPLIT"]),
        source,
        resolved,
        out,
        "--resolved-placement",
        "editor",
        "--require",
        "normal-completion",
        "--host-contract",
        profile,
        "--deployment-origin",
        origin,
        "--structural",
    ]
    verdicts = []
    for stage, args in [("emit", []), ("check-output", ["--check-output"])]:
        run = subprocess.run([*command, *args], capture_output=True, text=True, check=False)
        (artifacts / f"{stage}.log").write_text(run.stdout + run.stderr)
        assert run.returncode == 0, run.stdout + run.stderr
        verdict = json.loads(run.stdout.strip().splitlines()[-1])
        assert verdict["status"] == "certified", verdict
        assert verdict["requested"] == "normal-completion", verdict
        assert verdict["guarantee"] == "normal-completion", verdict
        assert verdict["scope"] == "linked-structural-moves", verdict
        assert verdict["hostProfile"] == "browser-page", verdict
        assert any(premise.startswith("Math.PI@1:") for premise in verdict["assumptions"]), verdict
        verdicts.append(verdict)
    assert verdicts[0] == verdicts[1]
    return {name: out / name for name in files}


def serve_file(url: str, file: Path) -> Route:
    assert file.is_file(), file

    async def get(_: Request) -> FileResponse:
        return FileResponse(file)

    return Route(f"/{url}", get)


def install_editor_overlay(app: Starlette, source: Path, files: dict[str, Path]) -> None:
    for name in files:
        if name != source.name and (source.parent / name).exists(follow_symlinks=False):
            raise ValueError(f"the emitted route shadows an original public asset ({name})")
    app.router.routes[:0] = [serve_file(name, path) for name, path in files.items()]


def replay_app(bundle: Path, index: Path, fonts: Path) -> Starlette:
    return Starlette(
        routes=[
            serve_file("", index),
            Mount("/fonts", app=StaticFiles(directory=fonts)),
            Mount("/", app=StaticFiles(directory=bundle, follow_symlink=True)),
        ]
    )


@pytest.fixture
def editor_app(runfiles: Runfiles, editor_source: Path) -> Starlette:
    return replay_app(
        editor_source.parent,
        bundled_file(runfiles, "EXCALIDRAW_INPUTS", "chunks") / "index.html",
        resolve(runfiles, os.environ["EXCALIDRAW_PACKAGE"]) / "dist/prod/fonts",
    )


@pytest.fixture(scope="session")
def editor_providers(runfiles: Runfiles, editor_source: Path) -> list[str]:
    metafile = bundled_file(runfiles, "EXCALIDRAW_OUTPUTS", "bundle_metadata.json")
    outputs = json.loads(metafile.read_text())["outputs"]
    (output,) = (path for path in outputs if PurePosixPath(path).name == editor_source.name)
    bundle = PurePosixPath(output).parent
    return [
        PurePosixPath(dependency["path"]).relative_to(bundle).as_posix()
        for dependency in outputs[output]["imports"]
        if dependency["kind"] == "import-statement" and not dependency.get("external")
    ]


async def smoke(
    playwright_driver: Playwright,
    origin: str,
    source: Path,
    providers: list[str],
    overlay: dict[str, Path] | None = None,
) -> list[str]:
    editor = source.name
    async with deterministic_browser_context(
        playwright_driver, viewport={"width": 1280, "height": 800}, frozen_now_ms=None
    ) as context:
        page = await context.new_page()
        errors: list[str] = []
        responses: dict[str, Response] = {}

        def on_console(message: ConsoleMessage) -> None:
            if message.type == "error":
                errors.append(message.text)

        def on_page_error(error: Error) -> None:
            errors.append(str(error))

        def on_response(response: Response) -> None:
            responses[response.url] = response

        def on_request_failed(request: BrowserRequest) -> None:
            errors.append(f"{request.url}: {request.failure}")

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)
        await page.goto(origin)
        assert await page.evaluate("location.origin") == origin
        await expect(page.locator(".App-toolbar")).to_be_visible()

        # Exercise the editor through its normal entry, then call the moved export.
        rectangle = page.get_by_test_id("toolbar-rectangle")
        await page.locator("label", has=rectangle).click()
        canvas = page.locator("canvas.excalidraw__canvas.interactive")
        box = await canvas.bounding_box()
        assert box is not None
        await canvas.hover(position={"x": 400, "y": 300})
        await page.mouse.down()
        await page.mouse.move(box["x"] + 600, box["y"] + 450, steps=5)
        await page.mouse.up()
        await expect(page.get_by_test_id("button-undo")).to_be_enabled()

        namespace = await page.evaluate(
            """async (path) => {
                const mod = await import(path);
                return { names: Object.keys(mod).sort(), scroll: mod.Bg({
                    scenePoint: { x: 30, y: 10 },
                    viewportDimensions: { width: 800, height: 600 },
                    zoom: { value: 2 },
                }) };
            }""",
            f"/{editor}",
        )
        assert len(namespace["names"]) == 611
        assert namespace["scroll"] == {"scrollX": 170, "scrollY": 140}
        await page.wait_for_load_state("networkidle")
        assert errors == []
        assert [url for url, response in responses.items() if response.status >= 400] == []
        expected_files = {name: source.parent / name for name in ["main.js", editor, *providers]}
        if overlay is not None:
            expected_files.update(overlay)
        for name, file in expected_files.items():
            assert await responses[f"{origin}/{name}"].body() == file.read_bytes(), name
        if overlay is not None:
            assert await page.evaluate(
                """async ([editor, scroll]) =>
                    Object.values(await import(scroll)).includes((await import(editor)).Bg)""",
                [f"/{editor}", f"/{SCROLL}"],
            )
            assert await page.evaluate(
                """async entry => {
                  const size = await import("/packages/excalidraw/element/sizehelpers.mjs");
                  const resize = await import("/packages/excalidraw/element/resizetest.mjs");
                  const editor = await import(entry);
                  const cursor = editor.Kj;
                  const visible = editor.Bj;
                  return Object.values(resize).includes(cursor) && Object.values(size).includes(visible)
                    && cursor({transformHandleType: "rotation"}) === "grab"
                    && cursor({element: {width: 20, height: 10, angle: Math.PI / 2}, transformHandleType: "n"}) === "ew-resize"
                    && cursor({element: {width: -20, height: 10, angle: 0}, transformHandleType: "nw"}) === "nesw-resize"
                    && size.B0("line", 149, 10).height === 0 && size.B0("rectangle", 13, -2).height === -13;
                }""",
                f"/{editor}",
            )
            # The editor's private rectanguloid helper now imports its upstream geometry constructors.
            sides, curves = await page.evaluate(
                """async () => {
                  const {Xs: deconstruct} = await import("/packages/excalidraw/element/utils.mjs");
                  return deconstruct({type: "rectangle", x: 10, y: 20, width: 30, height: 40, roundness: null}, 2);
                }"""
            )
            assert curves == []
            assert sides == [[[8, 18], [42, 18]], [[42, 18], [42, 62]], [[8, 62], [42, 62]], [[8, 62], [8, 18]]]
        probe = await page.evaluate(
            """async ([entry, moved]) => {
                const editor = await import(entry);
                const rect = {id: "box", type:"rectangle", x:0, y:0, width:100, height:80, angle:0, locked:false};
                const map = new Map([[rect.id, rect]]), zoom = {value:1}, device = {viewport:{isMobile:false}, isTouchScreen:false};
                const handles = editor.Gj(rect, zoom, map, "mouse", {});
                const [x,y,w,h] = handles.nw;
                const at = (state) => editor.Ij([rect], {selectedElementIds:state}, x+w/2,y+h/2,zoom,"mouse",map,device);
                const hit = at({box:true});
                const fromCoords = (sx,sy) => editor.Jj([0,0,100,80],sx,sy,zoom,"mouse",device);
                const same = moved ? Object.values(await import("/packages/excalidraw/element/transformhandles.mjs")) : Object.values(editor);
                return [same.includes(editor.Fj), same.includes(editor.Gj), same.includes(editor.Hj), w,h,
                    hit?.transformHandleType,hit?.element === rect,at({}) === null,fromCoords(x+w/2,y+h/2),
                    fromCoords(50,40),fromCoords(104,40),editor.Ij([rect],{selectedElementIds:{box:true}},104,40,zoom,"mouse",map,device)?.transformHandleType,editor.Hj([rect],{editingLinearElement:false})];
            }""",
            [f"/{editor}", overlay is not None],
        )
        assert probe == [True, True, True, 8, 8, "nw", True, True, "nw", False, "e", "e", True]
        return list(namespace["names"])


async def test_original_editor_replacement_routes(tmp_path: Path) -> None:
    source = tmp_path / "original" / "chunk-HASH.js"
    source.parent.mkdir()
    source.write_text("export const original = true;")
    (source.parent / "provider.js").write_text("export const provider = true;")
    index = tmp_path / "index.html"
    index.write_text("<!doctype html>")
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    certified = tmp_path / "certified"
    (certified / SCROLL).parent.mkdir(parents=True)
    (certified / SCROLL).write_text("export const scroll = true;")
    (certified / source.name).write_text("export const replacement = true;")
    (certified / "provider.js").write_text("export const provider = false;")

    overlay = {name: certified / name for name in (source.name, SCROLL)}
    app = replay_app(source.parent, index, fonts)
    async with serve_app_in_loop(app) as origin, httpx.AsyncClient(base_url=origin, trust_env=False) as client:
        assert (await client.get(f"/{source.name}")).content == source.read_bytes()
        assert (await client.get(f"/{SCROLL}")).status_code == 404
        install_editor_overlay(app, source, overlay)
        for name, file in {
            source.name: certified / source.name,
            SCROLL: certified / SCROLL,
            "provider.js": source.parent / "provider.js",
        }.items():
            response = await client.get(f"/{name}")
            assert str(response.url) == f"{origin}/{name}"
            assert response.status_code == 200
            assert response.content == file.read_bytes()

    existing = source.parent / SCROLL
    existing.parent.mkdir(parents=True)
    existing.write_text("export const original = true;")
    with pytest.raises(ValueError, match="shadows an original public asset"):
        install_editor_overlay(app, source, overlay)


async def test_original_editor(
    playwright_driver: Playwright, editor_app: Starlette, editor_source: Path, editor_providers: list[str]
) -> None:
    async with serve_app_in_loop(editor_app) as origin:
        await smoke(playwright_driver, origin, editor_source, editor_providers)


async def test_native_metadata_preset(
    playwright_driver: Playwright, runfiles: Runfiles, editor_app: Starlette, tmp_path: Path
) -> None:
    source = tmp_path / "record.mjs"
    source.write_text(
        "var read = () => import.meta.env.MODE; var meta = () => import.meta; "
        "var render = () => 42; export { read, meta, render };"
    )
    worker = "renderer.mjs"
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {"entry": source.name, "modules": {source.name: ["#0", "#1", "#3"], worker: ["#2"]}, "order": [worker]}
        )
    )
    profile = tmp_path / "host.json"
    profile.write_text(json.dumps({"profile": "browser-page"}))
    out = tmp_path / "certified"
    editor_app.router.routes.insert(0, serve_file(source.name, source))
    async with serve_app_in_loop(editor_app) as origin:
        command: list[str | Path] = [
            resolve(runfiles, os.environ["LOWERSPLIT"]),
            source,
            spec,
            out,
            "--structural",
            "--host-contract",
            profile,
            "--deployment-origin",
            origin,
            "--deployment-preset",
            "native-browser-record@1",
        ]
        for args in [[], ["--check-output"]]:
            run = await asyncio.to_thread(
                subprocess.run, [*command, *args], capture_output=True, text=True, check=False
            )
            assert run.returncode == 0, run.stdout + run.stderr
            assert json.loads(run.stdout)["scope"] == "linked-structural-pivot"

        for overlay in [False, True]:
            if overlay:
                install_editor_overlay(editor_app, source, {name: out / name for name in [source.name, worker]})
            async with deterministic_browser_context(
                playwright_driver, viewport={"width": 800, "height": 600}, frozen_now_ms=None
            ) as context:
                page = await context.new_page()
                await page.goto(origin)
                observation = await page.evaluate("""async () => {
                    const api = await import("/record.mjs");
                    const m = api.meta();
                    const desc = x => Object.values(Object.getOwnPropertyDescriptors(x)).map(
                        d => [typeof d.value, d.writable, d.enumerable, d.configurable]);
                    const initial = [Object.keys(m), desc(m), Object.getPrototypeOf(m) === null,
                        m.url, m.resolve.name, m.resolve.length, m.resolve("./asset.js")];
                    let current = {}, calls = 0;
                    Object.defineProperty(m, "env", {get() {
                        if (this !== m) throw new Error("wrong getter receiver");
                        calls++; return {MODE: current};
                    }});
                    const first = api.read(); current = {}; const second = api.read();
                    return [Object.keys(api), api.render(), initial, first !== second, second === current, calls];
                }""")
                assert observation == [
                    ["meta", "read", "render"],
                    42,
                    [
                        ["url", "resolve"],
                        [["string", True, True, True], ["function", True, True, True]],
                        True,
                        origin + "/record.mjs",
                        "resolve",
                        1,
                        origin + "/asset.js",
                    ],
                    True,
                    True,
                    2,
                ]


async def test_certified_editor_split(
    playwright_driver: Playwright,
    runfiles: Runfiles,
    editor_app: Starlette,
    editor_source: Path,
    editor_providers: list[str],
) -> None:
    async with serve_app_in_loop(editor_app) as origin:
        files = await asyncio.to_thread(certify_editor_split, runfiles, editor_source, origin)
        original = await smoke(playwright_driver, origin, editor_source, editor_providers)
        # The first browser context has closed. Switch only the certified routes on the running server.
        install_editor_overlay(editor_app, editor_source, files)
        assert await smoke(playwright_driver, origin, editor_source, editor_providers, files) == original


if __name__ == "__main__":
    pytest_bazel.main()
