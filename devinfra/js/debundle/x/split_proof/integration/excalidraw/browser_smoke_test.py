"""Run the original and certified splits of the pinned Excalidraw editor."""

# gazelle:include_dep //testing/frontend_visual:fixtures

import asyncio
import hashlib
import json
import math
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

RENDERER = "packages/excalidraw/renderer/interactivescene"
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


def certify_deployment(runfiles: Runfiles, editor: Path, origin: str, pinned: dict[str, str]) -> dict[str, Path]:
    sources = {"main": editor.parent / "main.js", "editor": editor}
    canonical = json.loads(resolve(runfiles, os.environ["CANONICAL_PLACEMENT"]).read_text())["placements"]
    selected = json.loads(resolve(runfiles, os.environ["SELECTED_PLACEMENT"]).read_text())["placements"]
    canonical_by_chunk = {placement["chunk_id"]: placement for placement in canonical}
    assert {placement["chunk_id"] for placement in selected} == set(canonical_by_chunk) == set(sources)

    chunks = []
    added_paths = set()
    for placement in selected:
        chunk = placement["chunk_id"]
        source = sources[chunk]
        full = canonical_by_chunk[chunk]
        assert placement["source_sha256"] == full["source_sha256"] == pinned[source.name]
        for ordinal, (unit, canonical_unit) in enumerate(zip(placement["units"], full["units"], strict=True)):
            assert unit["ordinal"] == canonical_unit["ordinal"] == ordinal
            assert unit["owner"] is None or unit["owner"] == canonical_unit["owner"], (chunk, ordinal)
        moved = [(unit["ordinal"], unit["owner"]) for unit in placement["units"] if unit["owner"] is not None]
        if chunk == "main":
            assert len(placement["units"]) == 1628
            assert placement["import_order"] == [RENDERER]
            assert moved == [(1486, RENDERER)]
        else:
            assert sum(len(unit.get("members", {})) for unit in placement["units"] if unit["owner"]) == 365
            assert len(placement["import_order"]) == 45
        for owner in placement["import_order"]:
            name = f"{owner}.mjs"
            assert name not in added_paths, name
            assert name not in pinned, name
            added_paths.add(name)
        chunks.append({"path": source.name, "source": str(source.absolute()), "placement": placement})

    artifacts = undeclared_outputs_dir()
    (artifacts / "browser-origin.txt").write_text(f"{origin}\n")
    (artifacts / "input-sha256.json").write_text(json.dumps(pinned, indent=2))
    deployment = artifacts / "deployment.json"
    deployment.write_text(json.dumps({"entry": "main.js", "chunks": chunks}))
    profile = artifacts / "host.json"
    profile.write_text(json.dumps({"profile": "browser-page", "stableIntrinsicValues": ["Math.PI@1"]}))
    out = artifacts / "certified"
    command: list[str | Path] = [
        resolve(runfiles, os.environ["LOWERSPLIT"]),
        deployment,
        out,
        "--require",
        "normal-completion",
        "--host-contract",
        profile,
        "--deployment-origin",
        origin,
        "--deployment-preset",
        "native-browser-record@1",
        "--deployment-import-map",
        "empty@1",
        "--structural",
    ]
    verdicts = []
    for stage, args in [("emit", []), ("check-output", ["--check-output"])]:
        run = subprocess.run([*command, *args], capture_output=True, text=True, check=False)
        (artifacts / f"{stage}.log").write_text(run.stdout + run.stderr)
        assert run.returncode == 0, run.stdout + run.stderr
        verdict = json.loads(run.stdout.strip().splitlines()[-1])
        assert verdict["status"] == "certified", verdict
        assert verdict["requested"] == verdict["guarantee"] == "normal-completion", verdict
        assert verdict["scope"] == "linked-structural-moves", verdict
        assert verdict["hostProfile"] == "browser-page", verdict
        for premise in ["Math.PI@1:", "native-browser-record@1:", "empty@1:"]:
            assert any(assumption.startswith(premise) for assumption in verdict["assumptions"]), verdict
        verdicts.append(verdict)
    assert verdicts[0] == verdicts[1]
    assert {path.relative_to(out).as_posix() for path in out.rglob("*") if path.is_file()} == set(pinned) | added_paths
    for name in set(pinned) - {path.name for path in sources.values()}:
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == pinned[name], name
    return {name: out / name for name in set(pinned) | added_paths}


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
    *,
    main_case: bool = False,
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
        if overlay is not None and editor in overlay:
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
            [f"/{editor}", overlay is not None and editor in overlay],
        )
        assert probe == [True, True, True, 8, 8, "nw", True, True, "nw", False, "e", "e", True]
        if main_case:
            assert await page.evaluate("async () => Object.keys(await import('/main.js'))") == []
            assert await page.locator("script[type=importmap]").count() == 0
            if overlay is not None:
                # The single private renderer is exported to main, not by main.
                calls = await page.evaluate(
                    """async file => {
                    const exports = Object.values(await import(file));
                    if (exports.length !== 1) throw new Error("unexpected renderer namespace");
                    const trace = [];
                    exports[0]({
                        beginPath: () => trace.push(["begin"]),
                        ellipse: (...args) => trace.push(["ellipse", ...args]),
                        stroke: () => trace.push(["stroke"]),
                    }, 12, 8, 20, 30, 0.25);
                    return trace;
                }""",
                    "/" + RENDERER + ".mjs",
                )
                assert calls == [["begin"], ["ellipse", 20, 30, 6, 4, 0.25, 0, 2 * math.pi], ["stroke"]]
            # This build deliberately externalizes the dialog library. Assert the
            # known rejection specifically; all other page failures still count.
            await page.evaluate("""() => {
                window.dialogFailures = [];
                addEventListener("unhandledrejection", e => {
                    if (e.reason instanceof TypeError && e.reason.message.includes("@excalidraw/mermaid-to-excalidraw")) {
                        window.dialogFailures.push(e.reason.name);
                        e.preventDefault();
                    }
                });
            }""")
            await page.get_by_title("More tools", exact=True).click()
            await page.get_by_text("Mermaid", exact=False).first.click()
            await expect(page.locator('.ttd-dialog[role="dialog"]')).to_be_visible()
            await page.wait_for_function("window.dialogFailures.length > 0")
            assert errors == []
        if overlay is not None and editor in overlay:
            geometry = await page.evaluate(
                """async () => {
                  const {al: line, K8: intersect} = await import("/packages/math/line.mjs");
                  const {ci: contains} = await import("/packages/math/triangle.mjs");
                  const horizontal = line([0, 2], [1, 2]);
                  const triangle = [[0, 0], [4, 0], [0, 4]];
                  return {
                    crossing: intersect(horizontal, line([3, 0], [3, 1])),
                    parallel: intersect(horizontal, line([0, 3], [1, 3])),
                    inside: contains(triangle, [1, 1]),
                    outside: contains(triangle, [3, 3]),
                  };
                }"""
            )
            assert geometry == {"crossing": [3, 2], "parallel": None, "inside": True, "outside": False}
            shape_results = await page.evaluate(
                """async () => {
                  const {A1: polygon, Er: fromPoints, C1: inside, t7: onEdge} = await import("/packages/math/polygon.mjs");
                  const {Ls: ellipse, Hu: intersect, Bu: distance} = await import("/packages/math/ellipse.mjs");
                  const pts = [[0,0],[4,0],[4,4],[0,4]];
                  const ring = polygon(...pts), e = ellipse([0,0], 2, 1);
                  return [ring.length, ring[0] === ring[4], fromPoints(ring) === ring,
                    inside([2,2],ring),inside([5,2],ring),onEdge([2,0],ring),onEdge([2,2],ring),
                    intersect(e,[[-3,0],[3,0]]).map(p=>p[0]).sort((a,b)=>a-b),
                    intersect(e,[[-3,1],[3,1]]).length,intersect(e,[[-3,2],[3,2]]).length,
                    Math.abs(distance([3,0], e) - 1) < 1e-4];
                }"""
            )
            assert shape_results == [5, True, True, True, False, True, False, [-2, 2], 1, 0, True]
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


async def test_bare_import(playwright_driver: Playwright, editor_app: Starlette, tmp_path: Path) -> None:
    name = "@fixture/diagram"
    entry = tmp_path / "entry.mjs"
    entry.write_text(f"var load = () => import({json.dumps(name)}); var render = () => 42; export {{load, render}};")
    target = tmp_path / "target.mjs"
    target.write_text("globalThis.evaluated++; export const value = 7;")
    html = tmp_path / "probe.html"
    html.write_text("<!doctype html>")
    for file in [html, entry, target]:
        editor_app.router.routes.insert(0, serve_file(file.name, file))
    async with serve_app_in_loop(editor_app) as origin:
        for mapped in (False, True):
            async with deterministic_browser_context(
                playwright_driver, viewport={"width": 800, "height": 600}, frozen_now_ms=None
            ) as context:
                page = await context.new_page()
                await page.goto(origin + "/" + html.name)
                if mapped:
                    await page.add_script_tag(type="importmap", content=json.dumps({"imports": {name: "./target.mjs"}}))
                observed = await page.evaluate("""async () => {
                  globalThis.evaluated = 0;
                  const api = await import("./entry.mjs");
                  const mark = globalThis.evaluated;
                  const a = api.load(), b = api.load();
                  const values = await Promise.allSettled([a,b]);
                  const results = values.map(x => x.status === "fulfilled" ? x.value.value : x.reason.name);
                  return [Object.keys(api), mark, a !== b, results, globalThis.evaluated];
                }""")
                assert observed == [
                    ["load", "render"],
                    0,
                    True,
                    [7, 7] if mapped else ["TypeError", "TypeError"],
                    int(mapped),
                ]
                fetched = await page.evaluate(
                    "url => performance.getEntriesByName(url, 'resource').length", origin + "/" + target.name
                )
                assert fetched == int(mapped)
                if not mapped:
                    # A caller that leaves the rejection unhandled exposes the reason
                    # and the actual promise through the browser rejection event.
                    event = await page.evaluate("""async () => {
                      const {load} = await import("./entry.mjs");
                      let pending;
                      const report = new Promise(resolve => addEventListener("unhandledrejection", e => {
                        e.preventDefault(); resolve([e.promise === pending, e.reason.name]);
                      }, {once:true}));
                      pending = load();
                      return report;
                    }""")
                    assert event == [True, "TypeError"]


async def test_native_metadata(playwright_driver: Playwright, editor_app: Starlette, tmp_path: Path) -> None:
    source = tmp_path / "record.mjs"
    source.write_text(
        "var read = () => import.meta.env.MODE; var meta = () => import.meta; "
        "var render = () => 42; export { read, meta, render };"
    )
    editor_app.router.routes.insert(0, serve_file(source.name, source))
    async with (
        serve_app_in_loop(editor_app) as origin,
        deterministic_browser_context(
            playwright_driver, viewport={"width": 800, "height": 600}, frozen_now_ms=None
        ) as context,
    ):
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


async def test_certified_deployment(
    playwright_driver: Playwright, runfiles: Runfiles, editor_app: Starlette, editor_source: Path
) -> None:
    pinned = json.loads(resolve(runfiles, os.environ["MAIN_STATIC_DIGESTS"]).read_text())
    for name, digest in pinned.items():
        assert hashlib.sha256((editor_source.parent / name).read_bytes()).hexdigest() == digest, name
    assert editor_source.name in pinned
    providers = sorted(set(pinned) - {"main.js", editor_source.name})
    async with serve_app_in_loop(editor_app) as origin:
        files = await asyncio.to_thread(certify_deployment, runfiles, editor_source, origin, pinned)
        original = await smoke(playwright_driver, origin, editor_source, providers, main_case=True)
        editor_app.router.routes[:0] = [serve_file(name, path) for name, path in files.items()]
        assert await smoke(playwright_driver, origin, editor_source, providers, files, main_case=True) == original


if __name__ == "__main__":
    pytest_bazel.main()
