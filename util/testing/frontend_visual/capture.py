"""Deterministic pages and screenshot publication for ordinary Playwright tests.

Tests own readiness, actions and assertions. Loading a harness does not drive it; capturing
never clicks, scrolls, or parks the pointer. The session owns health checks and output identity.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Literal, Self
from urllib.parse import urlencode

from more_itertools import one
from playwright.async_api import BrowserContext, Locator, Page, Playwright, Route
from pydantic import JsonValue, TypeAdapter
from python.runfiles import Runfiles

from devinfra.testing.output_writer import BoundTestOutputs
from util.testing.frontend_visual.context import DISABLE_ANIMATIONS_CSS, FROZEN_NOW_MS, deterministic_browser_context
from util.testing.frontend_visual.page_capture import (
    WAIT_TIMEOUT_MS,
    DevtoolsViewport,
    PageErrors,
    RequestFence,
    assert_network_settled,
    hidden_caret,
    repaint,
    screenshot_locator,
    wait_for_stable,
)
from util.testing.frontend_visual.viewports import Viewport

# `document.fonts.check` is true for a family no `@font-face` declares, so on its own it passes when the
# stylesheet declaring the font never arrived (or the name is misspelled) and the page renders in a fallback.
# A face loads only once laid-out text uses it, so this is asked of a mounted, painted scene, after
# `document.fonts.ready` has covered a load that frame started.
_FONT_STATUS_JS = """async family => {
    await document.fonts.ready;
    const declared = Array.from(document.fonts).some((face) => face.family.replace(/^["']|["']$/g, "") === family);
    if (!declared) return "undeclared";
    return document.fonts.check(`16px "${family}"`) ? "loaded" : "unloaded";
}"""


def _trace_enabled(case_id: str) -> bool:
    trace_setting = os.environ.get("VISUAL_TRACE", "1")
    if trace_setting not in {"0", "1"}:
        raise ValueError("VISUAL_TRACE must be 0 or 1")
    trace_capture_name = os.environ.get("VISUAL_TRACE_CAPTURE_NAME")
    trace_enabled = trace_setting == "1"
    if trace_capture_name is not None:
        trace_enabled = trace_enabled and trace_capture_name == case_id
    return trace_enabled


@asynccontextmanager
async def playwright_trace(
    context: BrowserContext, outputs: BoundTestOutputs, checkpoint: str | None = None
) -> AsyncIterator[None]:
    """Trace a named visual case under the shared environment and output policy."""
    if not _trace_enabled(outputs.case_id):
        yield
        return
    staging_dir = Path(tempfile.mkdtemp(prefix="visual-trace-"))
    trace_path = staging_dir / "trace.zip"
    retain_raw_trace = False
    try:
        await context.tracing.start(
            name=f"visual capture: {outputs.case_id}", screenshots=True, snapshots=True, sources=False
        )
        try:
            yield
        finally:
            active_error = sys.exception()
            try:
                await context.tracing.stop(path=trace_path)
                outputs.publish_file(source=trace_path, checkpoint=checkpoint, role="trace")
            except Exception as trace_error:
                if active_error is None:
                    raise
                note = (
                    f"Could not save Playwright trace for {outputs.case_id!r}, checkpoint {checkpoint!r}: "
                    f"{trace_error!r}"
                )
                if trace_path.is_file():
                    try:
                        outputs.output_dir.mkdir(parents=True, exist_ok=True)
                        recovery_dir = Path(tempfile.mkdtemp(prefix="trace-recovery-", dir=outputs.output_dir))
                        recovery_path = recovery_dir / "trace.zip"
                        shutil.copyfile(trace_path, recovery_path)
                    except OSError as recovery_error:
                        retain_raw_trace = True
                        note += (
                            f"; could not copy raw trace to test outputs: {recovery_error!r}; raw trace at {trace_path}"
                        )
                    else:
                        note += f"; raw trace retained at {recovery_path}"
                active_error.add_note(note)
    finally:
        if not retain_raw_trace:
            shutil.rmtree(staging_dir)


# Keep the browser-side snapshot in a linted JS file. It records geometry and selection positions,
# never page text, input values, URLs, or accessible names.
_CAPTURE_DIAGNOSTICS_JS = (
    Runfiles.CreateOrRaise().root() / "ducktape/util/testing/frontend_visual/assets/capture_diagnostics.js"
).read_text(encoding="utf-8")


_PATHS_BY_URL = TypeAdapter(dict[str, str])


@dataclass(frozen=True)
class InlinePage:
    """What a harness loaded with `set_content` is assembled from, besides the bundle."""

    stylesheet_paths: tuple[Path, ...]
    base_href: str | None
    # None: loaded with `set_content`, into a page of no origin. A URL: the fence answers the navigation to it
    # with the document, so the page has that origin and what hangs on one (storage, a cross-origin frame).
    url: str | None = None

    @classmethod
    def from_env(cls) -> InlinePage:
        """What `py_visual_test(inline_page = True)` sets: runfiles paths (`rlocationpath`) of the stylesheets, space-separated."""
        runfiles_root = Runfiles.CreateOrRaise().root()
        return cls(
            stylesheet_paths=tuple(runfiles_root / path for path in os.environ["STYLESHEET_PATHS"].split()),
            base_href=os.environ.get("BASE_HREF"),
            url=os.environ.get("PAGE_URL"),
        )


@dataclass(frozen=True)
class HarnessConfig:
    harness_path: Path
    title: str
    expected_font_family: str | None
    # None: the harness is the `file://` page beside its bundle.
    inline_page: InlinePage | None = None
    devtools_viewport: bool = False
    # URL prefix -> the HTML file the request fence answers it with; see `RequestFence`.
    served_documents: Mapping[str, Path] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> HarnessConfig:
        """What `py_visual_test` sets: runfiles paths (`rlocationpath`) of harness assets and capture metadata."""
        runfiles_root = Runfiles.CreateOrRaise().root()
        return cls(
            harness_path=runfiles_root / os.environ["HARNESS_PATH"],
            title=os.environ["VISUAL_TITLE"],
            expected_font_family=os.environ.get("EXPECTED_FONT_FAMILY"),
            inline_page=InlinePage.from_env() if os.environ.get("INLINE_PAGE") else None,
            devtools_viewport=bool(os.environ.get("DEVTOOLS_VIEWPORT")),
            served_documents={
                url: (runfiles_root / path)
                for url, path in _PATHS_BY_URL.validate_json(os.environ.get("SERVED_DOCUMENTS", "{}")).items()
            },
        )

    @property
    def harness_url(self) -> str:
        """The harness page: `index.html` beside the bundle, or beside the `dist/` directory holding it."""
        directory = self.harness_path.parent
        index = (directory.parent if directory.name == "dist" else directory) / "index.html"
        if not index.exists():
            raise FileNotFoundError(f"harness index.html not found for {self.harness_path}")
        # absolute(), not resolve(): the page pulls its bundle by relative path, which only exists beside
        # the runfiles symlink, not beside whatever it points to.
        return index.absolute().as_uri()

    @property
    def bundle_script(self) -> str:
        """The bundle's JavaScript: the file itself, or the one `.js` an esbuild directory output holds."""
        return (one(self.harness_path.glob("*.js")) if self.harness_path.is_dir() else self.harness_path).read_text(
            encoding="utf-8"
        )


def inline_page_html(page: InlinePage, *, bundle_script: str, window_globals: Mapping[str, JsonValue] | None) -> str:
    """The document `set_content` loads: stylesheets, then `window_globals`, then the bundle.

    The animation-pinning CSS goes in with the stylesheets so it is in effect before anything mounts.
    """
    css = "".join(path.read_text(encoding="utf-8") for path in page.stylesheet_paths) + DISABLE_ANIMATIONS_CSS
    assignments = f"Object.assign(window, {_script_literal(dict(window_globals or {}))});"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        + (f"<base href='{page.base_href}'>" if page.base_href else "")
        + f"<style>{css}</style></head><body><div id='app'></div>"
        + f"<script>{assignments}</script><script>{bundle_script}</script></body></html>"
    )


def _script_literal(value: JsonValue) -> str:
    # `<` is escaped so a string value cannot close the script element.
    return json.dumps(value).replace("<", "\\u003c")


class VisualPage:
    """An instrumented page; enter before navigation to check errors for its whole lifetime."""

    def __init__(
        self,
        page: Page,
        *,
        outputs: BoundTestOutputs,
        title: str,
        fence: RequestFence | None = None,
        devtools_viewport: DevtoolsViewport | None = None,
        expected_font_family: str | None = None,
    ) -> None:
        self.page = page
        self.errors = PageErrors(page)
        self.outputs = outputs
        self.title = title
        self.fence = fence
        self.devtools_viewport = devtools_viewport
        self.expected_font_family = expected_font_family

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        try:
            self.errors.assert_none(context=self.case_id)
        except AssertionError as page_error:
            if exc is None:
                raise
            exc.add_note(str(page_error))

    @property
    def output_dir(self) -> Path:
        return self.outputs.output_dir

    @property
    def case_id(self) -> str:
        return self.outputs.case_id

    async def check(self, *, context: str) -> None:
        self.errors.assert_none(context=context)
        await assert_network_settled(self.page, context=context)
        await wait_for_stable(self.page)
        if self.fence is not None:
            self.fence.assert_none_escaped(context=context)
        self.errors.assert_none(context=context)

    async def capture(
        self,
        name: str | None = None,
        *,
        target: Locator | None = None,
        label: str | None = None,
        full_page: bool = False,
        scale: Literal["css", "device"] = "device",
        animations: Literal["allow", "disabled"] = "allow",
    ) -> Path:
        checkpoint = name
        context = f"{self.case_id}::{checkpoint}" if checkpoint is not None else self.case_id
        if target is not None and full_page:
            raise ValueError("choose an element or a full page, not both")
        await self.check(context=context)
        if self.expected_font_family:
            status = await self.page.evaluate(_FONT_STATUS_JS, self.expected_font_family)
            if status != "loaded":
                raise AssertionError(f"{context}: {self.expected_font_family} font did not load ({status})")
        if (
            target is None
            and self.devtools_viewport is not None
            and not full_page
            and scale == "device"
            and animations != "allow"
        ):
            raise ValueError("DevTools captures require animations to be pinned before mount")
        await repaint(self.page)
        if target is not None and (count := await target.count()) != 1:
            raise ValueError(f"{context}: screenshot target must match exactly one element, got {count}")
        page_diagnostics = (
            await target.evaluate(_CAPTURE_DIAGNOSTICS_JS)
            if target is not None
            else await self.page.evaluate(_CAPTURE_DIAGNOSTICS_JS)
        )
        if target is not None:
            screenshot = await screenshot_locator(
                self.page, target, context=context, scale=scale, animations=animations
            )
        elif self.devtools_viewport is not None and not full_page and scale == "device":
            async with hidden_caret(self.page):
                screenshot = await self.devtools_viewport.screenshot()
        else:
            screenshot = await self.page.screenshot(full_page=full_page, scale=scale, animations=animations)
        await self.check(context=context)
        diagnostics = {
            "case_id": self.case_id,
            "checkpoint": checkpoint,
            "sample": "before_screenshot",
            "scale": scale,
            "full_page": full_page,
            **page_diagnostics,
        }
        self.outputs.publish(
            checkpoint=checkpoint,
            role="diagnostics",
            data=json.dumps(diagnostics, ensure_ascii=False, separators=(",", ":")),
            suffix="json",
        )
        return self.outputs.publish(
            checkpoint=checkpoint,
            role="screenshot",
            data=screenshot,
            suffix="png",
            label=label or checkpoint or self.case_id,
            title=self.title,
        )


@asynccontextmanager
async def open_visual_page(
    context: BrowserContext, outputs: BoundTestOutputs, *, title: str, trace: bool = False
) -> AsyncIterator[VisualPage]:
    """Create a monitored page in an existing context, before any navigation.

    The caller owns the browser context and can configure routes, cookies, and other
    app-specific state on the yielded page. Tracing follows the shared output policy.
    """
    async with AsyncExitStack() as stack:
        if trace:
            await stack.enter_async_context(playwright_trace(context, outputs))
        page = await context.new_page()
        stack.push_async_callback(page.close)
        with VisualPage(page, outputs=outputs, title=title) as view:
            yield view


class VisualHarness:
    def __init__(self, playwright: Playwright, config: HarnessConfig, outputs: BoundTestOutputs) -> None:
        self.playwright = playwright
        self.config = config
        self.outputs = outputs

    @property
    def output_dir(self) -> Path:
        return self.outputs.output_dir

    @property
    def case_id(self) -> str:
        return self.outputs.case_id

    @asynccontextmanager
    async def open(
        self,
        *,
        viewport: Viewport | None = None,
        color_scheme: Literal["light", "dark"] = "light",
        query: Mapping[str, str] | None = None,
        window_globals: Mapping[str, JsonValue] | None = None,
        frozen_now_ms: int = FROZEN_NOW_MS,
        trace_checkpoint: str | None = None,
    ) -> AsyncIterator[VisualPage]:
        config = self.config
        viewport = viewport or Viewport()
        if config.inline_page is None and window_globals is not None:
            raise ValueError("window globals require an inline harness")
        if config.inline_page is not None and query is not None:
            raise ValueError("an inline harness is selected by window globals, not query")
        async with (
            deterministic_browser_context(
                self.playwright,
                viewport=viewport.size,
                frozen_now_ms=frozen_now_ms,
                color_scheme=color_scheme,
                device_scale_factor=viewport.device_scale_factor,
                has_touch=viewport.has_touch,
                extra_args=["--allow-file-access-from-files"] if config.inline_page is None else [],
            ) as context,
            playwright_trace(context, self.outputs, trace_checkpoint),
        ):
            page = await context.new_page()
            page.set_default_timeout(WAIT_TIMEOUT_MS)
            devtools = (
                await DevtoolsViewport.attach(
                    page, width=viewport.width, height=viewport.height, device_scale_factor=viewport.device_scale_factor
                )
                if config.devtools_viewport
                else None
            )
            fence = RequestFence(
                (lambda request: request.url.startswith("file://"))
                if config.inline_page is None
                else (lambda _: False),
                served_documents={
                    url: path.read_text(encoding="utf-8") for url, path in config.served_documents.items()
                },
            )
            with VisualPage(
                page,
                outputs=self.outputs,
                title=config.title,
                fence=fence,
                devtools_viewport=devtools,
                expected_font_family=config.expected_font_family,
            ) as view:
                await fence.install(page)
                if config.inline_page is None:
                    url = config.harness_url
                    if query:
                        url += f"?{urlencode(query)}"
                    await page.goto(url, wait_until="domcontentloaded")
                else:
                    html = inline_page_html(
                        config.inline_page, bundle_script=config.bundle_script, window_globals=window_globals
                    )
                    if config.inline_page.url is None:
                        await page.set_content(html, wait_until="load")
                    else:

                        async def fulfill(route: Route) -> None:
                            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

                        await page.route(config.inline_page.url, fulfill)
                        await page.goto(config.inline_page.url, wait_until="load")
                yield view
                await view.check(context=self.case_id)
