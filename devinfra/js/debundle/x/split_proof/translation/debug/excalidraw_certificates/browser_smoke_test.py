"""Run the pinned Excalidraw editor with the original or certified scroll module."""

# gazelle:include_dep //testing/frontend_visual:fixtures

import json
import os
import subprocess
from pathlib import Path

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
from testing.asgi import serve_app
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


def certify_scroll(runfiles: Runfiles, source: Path) -> Path:
    authored = json.loads(resolve(runfiles, os.environ["EDITOR_LEAN_SPEC"]).read_text())
    assert authored["modules"][SCROLL] == ["#1337", "#1338"]
    selectors = [unit for units in authored["modules"].values() for unit in units]
    source_order = [f"#{index}" for index in range(1787)]
    assert len(selectors) == len(source_order)
    assert set(selectors) == set(source_order)
    artifacts = undeclared_outputs_dir()
    spec = artifacts / "scroll.json"
    # Keep the public URL of the editor so main.js can use its original imports.
    spec.write_text(
        json.dumps(
            {
                "entry": source.name,
                "modules": {source.name: [unit for unit in source_order if unit != "#1337"], SCROLL: ["#1337"]},
                "order": [SCROLL],
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
    ]
    verdicts = []
    for stage, args in [("emit", []), ("check-output", ["--check-output"])]:
        run = subprocess.run([*command, *args], capture_output=True, text=True, check=False)
        (artifacts / f"{stage}.log").write_text(run.stdout + run.stderr)
        assert run.returncode == 0, run.stdout + run.stderr
        verdict = json.loads(run.stdout.strip().splitlines()[-1])
        assert verdict["status"] == "certified", verdict
        assert verdict["requested"] == "normal-completion", verdict
        assert verdict["guarantee"] in ("normal-completion", "all-outcomes"), verdict
        assert verdict["scope"] == "linked-programs", verdict
        assert verdict["hostProfile"] == "browser-page", verdict
        assert isinstance(verdict["assumptions"], list), verdict
        verdicts.append(verdict)
    assert verdicts[0] == verdicts[1]
    return out


def serve_file(url: str, file: Path) -> Route:
    assert file.is_file(), file

    async def get(_: Request) -> FileResponse:
        return FileResponse(file)

    return Route(f"/{url}", get)


async def smoke(
    playwright_driver: Playwright, runfiles: Runfiles, source: Path, certified: Path | None = None
) -> list[str]:
    editor = source.name
    app = Starlette(
        routes=[
            serve_file("", bundled_file(runfiles, "EXCALIDRAW_INPUTS", "chunks") / "index.html"),
            *([serve_file(name, certified / name) for name in (editor, SCROLL)] if certified is not None else []),
            Mount(
                "/fonts",
                app=StaticFiles(directory=resolve(runfiles, os.environ["EXCALIDRAW_PACKAGE"]) / "dist/prod/fonts"),
            ),
            Mount("/", app=StaticFiles(directory=source.parent, follow_symlink=True)),
        ]
    )
    async with (
        serve_app(app) as origin,
        deterministic_browser_context(
            playwright_driver, viewport={"width": 1280, "height": 800}, frozen_now_ms=None
        ) as context,
    ):
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
        if certified is not None:
            for name in (editor, SCROLL):
                # The replacement and scroll module must be loaded from the exact checked files.
                assert await responses[f"{origin}/{name}"].body() == (certified / name).read_bytes()
        return list(namespace["names"])


async def test_original_editor(playwright_driver: Playwright, runfiles: Runfiles, editor_source: Path) -> None:
    await smoke(playwright_driver, runfiles, editor_source)


async def test_certified_scroll(playwright_driver: Playwright, runfiles: Runfiles, editor_source: Path) -> None:
    certified = certify_scroll(runfiles, editor_source)
    original = await smoke(playwright_driver, runfiles, editor_source)
    assert await smoke(playwright_driver, runfiles, editor_source, certified) == original


if __name__ == "__main__":
    pytest_bazel.main()
