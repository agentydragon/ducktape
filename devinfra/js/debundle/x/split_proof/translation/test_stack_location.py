"""Relocating an initializer can change a property key stored on the browser global."""

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Browser, Route, sync_playwright
from python.runfiles import Runfiles

from testing.browser.launcher import CONTAINER_BASE_BROWSER_ARGS, chromium_executable

ORIGIN = "https://example.test"
ENTRY = "main.mjs"
OWNER = "parts/register.mjs"


def registry(browser: Browser, modules: dict[str, str]) -> dict[str, int]:
    def serve(route: Route) -> None:
        name = urlparse(route.request.url).path.lstrip("/")
        if not name:
            route.fulfill(content_type="text/html", body="<!doctype html><title>Registry</title>")
        elif name in modules:
            route.fulfill(content_type="text/javascript", body=modules[name])
        else:
            route.abort()

    with browser.new_context() as context:
        page = context.new_page()
        page.route(f"{ORIGIN}/**", serve)
        page.goto(ORIGIN)
        contents: dict[str, int] = page.evaluate(
            """async (entry) => {
                globalThis.registry = {};
                await import(entry);
                return globalThis.registry;
            }""",
            f"/{ENTRY}",
        )
        return contents


def test_relocated_stack_key(tmp_path: Path) -> None:
    source = tmp_path / ENTRY
    source.write_text("globalThis.registry[new Error().stack] = 1;\n")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"entry": ENTRY, "modules": {ENTRY: [], OWNER: ["#0"]}, "order": [OWNER]}))
    out = tmp_path / "split"
    splitter = Runfiles.CreateOrRaise().Rlocation(os.environ["LOWERSPLIT"])
    assert splitter is not None
    args = [str(source), str(spec), str(out)]
    checked = subprocess.run(
        [splitter, *args, "--require", "normal-completion"], text=True, capture_output=True, check=False
    )
    assert checked.returncode == 1, checked.stderr
    assert json.loads(checked.stdout)["status"] == "rejected"
    assert not out.exists()

    preview = subprocess.run([splitter, "--diagnostic", *args, "--force"], text=True, capture_output=True, check=False)
    assert preview.returncode in (0, 1), preview.stderr
    files = {name: (out / name).read_text() for name in [ENTRY, OWNER]}
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=chromium_executable(), args=CONTAINER_BASE_BROWSER_ARGS)
        original = registry(browser, {ENTRY: source.read_text()})
        assert registry(browser, {ENTRY: source.read_text()}) == original
        split = registry(browser, files)
        browser.close()

    (original_key,) = original
    (split_key,) = split
    assert original[original_key] == split[split_key] == 1
    assert f"{ORIGIN}/{ENTRY}:" in original_key.splitlines()[1]
    assert f"{ORIGIN}/{OWNER}:" in split_key.splitlines()[1]
    assert original_key not in split
