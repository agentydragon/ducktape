"""Run the original and certified splits of the pinned Excalidraw editor."""

# gazelle:include_dep //testing/frontend_visual:fixtures

import asyncio
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
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

COORDINATES = "packages/excalidraw/scene/coordinates.mjs"
SCROLL = "packages/excalidraw/scene/scroll.mjs"
STARTUP = "packages/excalidraw/editor/startup.mjs"
STARTUP_UNITS = range(5, 19)
# Keep interleaved computed initializers in the entry and put #404 with the font helpers.
EDITOR_CONSTANT_UNITS = tuple(
    unit for unit in range(385, 472) if unit not in {392, 403, 404, 409, 413, 415, 422, 423, 426, 445, 459, 462, 463}
)


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


def certify_editor_split(
    runfiles: Runfiles, source: Path, origin: str, placement: Mapping[str, Sequence[int]], expected_scope: str
) -> dict[str, Path]:
    authored = json.loads(resolve(runfiles, os.environ["EDITOR_LEAN_SPEC"]).read_text())
    assert authored["modules"][SCROLL] == ["#1337", "#1338"]
    selectors = [unit for units in authored["modules"].values() for unit in units]
    source_order = [f"#{index}" for index in range(1787)]
    assert len(selectors) == len(source_order)
    assert set(selectors) == set(source_order)
    artifacts = undeclared_outputs_dir()
    (artifacts / "browser-origin.txt").write_text(f"{origin}\n")
    spec = artifacts / "placement.json"
    modules = {name: [f"#{index}" for index in ordinals] for name, ordinals in placement.items()}
    moved = {unit for units in modules.values() for unit in units}
    # Keep the public URL of the editor so main.js can use its original imports.
    spec.write_text(
        json.dumps(
            {
                "entry": source.name,
                "modules": {source.name: [unit for unit in source_order if unit not in moved], **modules},
                "order": list(modules),
                "sequential": True,
            }
        )
    )
    profile = artifacts / "host.json"
    profile.write_text(json.dumps({"profile": "browser-page"}))
    out = artifacts / "certified-editor"
    command: list[str | Path] = [
        resolve(runfiles, os.environ["LOWERSPLIT"]),
        source,
        spec,
        out,
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
        assert verdict["scope"] == expected_scope, verdict
        assert verdict["hostProfile"] == "browser-page", verdict
        assert isinstance(verdict["assumptions"], list), verdict
        verdicts.append(verdict)
    assert verdicts[0] == verdicts[1]
    return {name: out / name for name in (source.name, *modules)}


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


@pytest.mark.parametrize(
    ("placement", "expected_scope"),
    [
        pytest.param({SCROLL: (1337,)}, "linked-structural-pivot", id="scroll"),
        pytest.param({STARTUP: STARTUP_UNITS}, "linked-structural-order", id="startup"),
        pytest.param(
            {
                STARTUP: STARTUP_UNITS,
                "packages/excalidraw/editor/codecs.mjs": range(19, 301),
                "packages/excalidraw/editor/utilities.mjs": range(301, 354),
                "packages/excalidraw/editor/palette-primitives.mjs": range(359, 367),
                "packages/excalidraw/editor/constants.mjs": EDITOR_CONSTANT_UNITS,
                "packages/excalidraw/editor/font-strings.mjs": (404, 548, 549),
                "packages/excalidraw/editor/frame-animation.mjs": (550, 551, 552, 553, 554),
                "packages/excalidraw/editor/collections.mjs": (
                    555,
                    568,
                    569,
                    577,
                    578,
                    579,
                    580,
                    589,
                    590,
                    595,
                    596,
                    597,
                ),
                COORDINATES: (559, 560),
                SCROLL: (1337,),
            },
            "linked-structural-moves",
            id="editor",
        ),
    ],
)
async def test_certified_editor_split(
    playwright_driver: Playwright,
    runfiles: Runfiles,
    editor_app: Starlette,
    editor_source: Path,
    editor_providers: list[str],
    placement: Mapping[str, Sequence[int]],
    expected_scope: str,
) -> None:
    async with serve_app_in_loop(editor_app) as origin:
        files = await asyncio.to_thread(
            certify_editor_split, runfiles, editor_source, origin, placement, expected_scope
        )
        original = await smoke(playwright_driver, origin, editor_source, editor_providers)
        # The first browser context has closed. Switch only the certified routes on the running server.
        install_editor_overlay(editor_app, editor_source, files)
        assert await smoke(playwright_driver, origin, editor_source, editor_providers, files) == original


if __name__ == "__main__":
    pytest_bazel.main()
