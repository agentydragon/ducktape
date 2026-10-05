"""Deterministic-screenshot primitives for a Playwright page: the Python counterpart of `frontend_visual/capture.mjs`.

Deviation from `capture.mjs`: nothing prepares the page. Viewport, colour scheme, reduced motion and
the frozen clock are context options, set by `frontend_visual.deterministic_browser_context`. What is
left here is what a page records or waits for, and each recorder is a value the caller holds, so
asserting on a page nobody instrumented cannot be written.

Loading content and orchestrating several shots stay with the caller: `visual_sweep` for a table of
`file://` harness scenes, a bespoke driver for anything else.
"""

from __future__ import annotations

import base64
import math
import re
from collections.abc import Callable

from playwright.async_api import (
    CDPSession,
    FloatRect,
    Page,
    Request,
    Route,
    TimeoutError as PlaywrightTimeoutError,  # the builtin TimeoutError is another type
)

# The bound every wait in a visual test takes: navigations, the mount wait, a scenario's own
# condition, the network-settle gate. Measured across a 49-target parallel uncached run: the
# slowest navigation to network idle took 1.9s and the slowest mount after it 79ms, so this is
# ~380x the slowest healthy mount seen under the load that produces flakes. It is no larger because
# a bound only helps while it is what reports the failure: a `small` target (60s) must fail here,
# naming the selector it waited for, rather than be killed by Bazel.
WAIT_TIMEOUT_MS = 30_000

_WAIT_FOR_STABLE_JS = """async () => {
    await document.fonts.ready;
    await Promise.all(
        Array.from(document.images)
            .filter((image) => !image.complete)
            // A broken src rejects; that is the page's problem to render, not ours to wait on.
            .map((image) => image.decode().catch(() => {}))
    );
    // Two frames: the first flushes pending style and layout, the second lands after paint.
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
}"""

# Polled once per frame. Quiet means `pending` stayed empty across a painted frame, so a response
# whose re-render immediately starts another fetch is drained rather than captured between the two.
# Three polls: empty, empty a frame later, empty a frame after that. The predicate is synchronous
# because Playwright does not await a promise a predicate returns, so its memory is a page global.
_LEDGER_QUIET_JS = """() => {
    const empty = window.__visualNetworkLedger__.pending.length === 0;
    window.__visualNetworkQuietPolls__ = empty ? window.__visualNetworkQuietPolls__ + 1 : 0;
    return window.__visualNetworkQuietPolls__ >= 3;
}"""


# The element's box in document coordinates, which is what a full-page clip is measured in.
_ELEMENT_BOX_JS = """element => {
    const { x, y, width, height } = element.getBoundingClientRect();
    return { x: x + window.scrollX, y: y + window.scrollY, width, height };
}"""


class DevtoolsViewport:
    """A page's viewport, emulated and captured over the DevTools protocol the way Puppeteer's `setViewport` and `screenshot` do.

    Deviation from the Playwright viewport (a context option, captured by `page.screenshot()`): Playwright
    emulates the metrics without setting the visible size and clips a viewport screenshot to the layout
    viewport, and at some device scale factors that rasterizes a few pixels of the capture differently from
    Puppeteer: the partly covered last row and column, and some anti-aliased edges. Measured on one page set:
    identical at a scale of 1 and 2, different at 1.5, 2.625 and 3. A lane whose images the Puppeteer sweep
    published uses this to keep them byte-identical; any other lane has no reason to. Attach before navigating.
    """

    def __init__(self, session: CDPSession) -> None:
        self._session = session

    @classmethod
    async def attach(cls, page: Page, *, width: int, height: int, device_scale_factor: float) -> DevtoolsViewport:
        session = await page.context.new_cdp_session(page)
        await session.send(
            "Emulation.setDeviceMetricsOverride",
            {"mobile": False, "width": width, "height": height, "deviceScaleFactor": device_scale_factor},
        )
        return cls(session)

    async def screenshot(self) -> bytes:
        """The viewport as drawn, in device pixels."""
        capture = await self._session.send(
            "Page.captureScreenshot",
            {"format": "png", "optimizeForSpeed": False, "fromSurface": True, "captureBeyondViewport": False},
        )
        return base64.b64decode(capture["data"])


async def wait_for_stable(page: Page) -> None:
    """Wait until the page is done rendering what it has: fonts applied, images decoded, a frame painted.

    Finishes as soon as those hold, and cannot pass early on a loaded runner the way a sleep can.
    Deliberately does not await `document.getAnimations()`: the sweep pins every animation with
    `animation-play-state: paused`, and a paused animation's `finished` never settles, so awaiting it
    would hang instead of capturing.

    It cannot know a scene's own readiness (data arriving, a component mounting lazily); wait for
    that with a selector on the thing the scene is about, then call this.
    """
    await page.evaluate(_WAIT_FOR_STABLE_JS)


class PageErrors:
    """Uncaught errors a page throws, to fail the capture on.

    With no pixel baseline to compare against, this is the primary crash detector: a component that
    throws out of an effect leaves a partial render that photographs perfectly well, and publishes as
    a plausible-looking baseline nobody re-reads. Construct it before loading content, so an error
    thrown during load is caught too.
    """

    def __init__(self, page: Page) -> None:
        self._errors: list[str] = []
        page.on("pageerror", lambda error: self._errors.append(error.stack or str(error)))

    def assert_none(self, *, context: str) -> None:
        if self._errors:
            raise AssertionError(f"{context}: uncaught page errors:\n  " + "\n  ".join(self._errors))


class RequestFence:
    """Abort and record every request the page makes that `allow` does not accept.

    For a page whose content is entirely local (`file://` assets, stubbed fetch), any escaping request
    is a hole in its hermeticity. In the test sandbox such a request can only fail, and its failure
    racing the capture is exactly how a transient error state gets published as a plausible-looking
    baseline: so the fence aborts it at once and the caller fails the scene by asserting on it before
    capturing. Install it before navigating.
    """

    def __init__(self, allow: Callable[[Request], bool]) -> None:
        self._allow = allow
        self._escaped: list[str] = []

    async def install(self, page: Page) -> None:
        await page.route(re.compile(r".*"), self._handle)

    async def _handle(self, route: Route) -> None:
        request = route.request
        if self._allow(request):
            await route.continue_()
            return
        self._escaped.append(f"{request.resource_type} {request.url}")
        await route.abort()

    def assert_none_escaped(self, *, context: str) -> None:
        if self._escaped:
            raise AssertionError(f"{context}: requests escaped the harness:\n    " + "\n    ".join(self._escaped))


async def assert_network_settled(page: Page, *, context: str, timeout_ms: int = WAIT_TIMEOUT_MS) -> None:
    """Wait until the harness's stubbed network is quiet, then fail on anything it recorded.

    The in-page half is `window.__visualNetworkLedger__`
    (haku/console/frontend/tool_rendering/screenshot/visual_network_ledger.ts), maintained by the
    harness's fetch stub: `pending` holds each stubbed fetch's URL while it is in flight, and
    `violations` records what must fail the run: an unmatched route, a rejected fetch, an unhandled
    promise rejection. A timeout names what was still in flight instead of reporting a bare timeout.

    A page with no ledger passes: a harness that stubs no fetch has nothing to settle, and
    `RequestFence` is what keeps such a page's network empty.
    """
    if not await page.evaluate("Boolean(window.__visualNetworkLedger__)"):
        return
    await page.evaluate("window.__visualNetworkQuietPolls__ = 0")
    try:
        await page.wait_for_function(_LEDGER_QUIET_JS, timeout=timeout_ms)
    except PlaywrightTimeoutError as timeout:
        if pending := await page.evaluate("window.__visualNetworkLedger__.pending"):
            raise AssertionError(
                f"{context}: requests still in flight after {timeout_ms}ms: {', '.join(pending)}"
            ) from timeout
        raise
    if violations := await page.evaluate("window.__visualNetworkLedger__.violations"):
        raise AssertionError(f"{context}: network violations:\n  " + "\n  ".join(violations))


async def screenshot_element(page: Page, selector: str, *, context: str) -> bytes:
    """Screenshot the element `selector` matches, failing by name when it matches none.

    The element's box is rounded to the nearest pixel, the way Puppeteer does, and not outward as
    Playwright's own element screenshot does: a fractional height such as 1630.4 would otherwise
    publish one row taller than the Puppeteer sweeps did, and every migrated image would then
    read as changed in PR visual review. An element taller than the viewport is captured whole.
    """
    if (element := await page.query_selector(selector)) is None:
        raise LookupError(f"{context}: {selector=} matched no element")
    box = await element.evaluate(_ELEMENT_BOX_JS)
    x, y = _round_half_up(box["x"]), _round_half_up(box["y"])
    clip: FloatRect = {
        "x": x,
        "y": y,
        "width": _round_half_up(box["width"] + box["x"] - x),
        "height": _round_half_up(box["height"] + box["y"] - y),
    }
    if clip["width"] == 0 or clip["height"] == 0:
        raise ValueError(f"{context}: {selector=} has no visible extent: {clip=}")
    return await page.screenshot(clip=clip, full_page=True)


def _round_half_up(value: float) -> int:
    # JavaScript's Math.round, which Puppeteer rounds with; Python's round() goes to the even neighbour.
    return math.floor(value + 0.5)
