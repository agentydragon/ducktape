# Python visual tests

Visual tests are ordinary async pytest tests that load a fixture and call the shared
screenshot API, optionally driving Playwright interactions and asserting behavior. A
mount-and-capture test is useful on its own: published before/after images in GitHub PR
comments let reviewers see what a UI change looks like. Readiness checks help capture the
intended state; behavioral assertions are additional coverage, not a prerequisite.
There is no JSON/jq instruction interpreter and no browser-side test driver.

## Responsibilities

The shared Chromium executable and launch flags live in `util/testing/browser/`; ordinary
Playwright fixtures use those without depending on visual capture policy. This package owns
deterministic contexts, render health, visual fixtures, and screenshot publication.

- **Tests** own fixture selection, readiness, actions, assertions, scrolling, pointer
  placement and screenshot checkpoints. Use normal functions and pytest parameterization.
  Fixture identifiers select data, not viewport, theme, readiness, or interactions. Reuse a
  fixture for multiple tests. Screenshot names identify outputs; do not dispatch behavior
  from their spelling or suffixes.
- **TS harnesses** mount production components and provide synthetic fixture data or fake
  services. A shared/generated fixture catalog may enumerate data; it must not contain
  selectors, clicks, waits, or capture instructions.
- **`VisualHarness` / `VisualPage`** (`util/testing/frontend_visual/capture.py`) own browser lifecycle,
  render-health checks, screenshot mechanics and review publication.
- **`py_visual_test`** owns Bazel runfiles, the Python entry point and the `visual` tag.

## Example

```python
import pytest
import pytest_bazel
from playwright.async_api import expect

from util.testing.frontend_visual.capture import VisualHarness

# gazelle:include_dep //util/testing/frontend_visual:fixtures
pytest_plugins = ("util.testing.frontend_visual.fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_details(visual: VisualHarness) -> None:
    async with visual.open() as view:
        await view.page.get_by_role("button", name="Details").click()
        panel = view.page.get_by_role("region", name="Details")
        await expect(panel).to_be_visible()
        await view.capture("details", target=panel)


if __name__ == "__main__":
    pytest_bazel.main()
```

Pass `test_module`, `test_srcs`, direct `test_deps`, bundled `harness`, `assets` and
`title` to `py_visual_test`. Exclude macro-owned test sources from Gazelle so it does
not create a second test target. Prefer one test source per target; put shared helpers in
Gazelle-managed libraries rather than loading sibling test modules into one runner.

`visual.open()` can load a harness without any fixture ID. For harnesses with callable
setup APIs, configure the mocks and mount the component from Python before asserting
readiness. Do not add a name-to-recipe registry just to open a page.

`visual.open()` accepts browser geometry (`util/testing/frontend_visual/viewports.py`), color scheme,
explicit query parameters and an optional frozen instant. Inline harnesses use `window_globals`
instead of a query. The macro's `page_url` gives an inline document an origin for
storage; `served_documents` supplies mock iframe documents.

The shared pytest plugin also provides a per-test `view` yield fixture, with ordinary
`viewport` and `color_scheme` fixtures that tests can override or parametrize. It opens
an unnamed harness and closes its isolated browser context after the test. App-specific
fixtures can depend on it to prepare reusable state without hiding test interactions.

For a real server or an otherwise app-owned browser context, use
`async with open_visual_page(context, test_outputs, title="...") as view:`. It creates
and closes the page, records load errors before navigation, and optionally enables the
shared trace policy with `trace=True`. The caller owns the context, cookies, routes,
and navigation.

The root pytest configuration provides `test_outputs`, a generic fixture bound to the full
repository-relative pytest node ID, including parameter IDs. It can publish any output role via
`test_outputs.publish(...)`; `test_outputs.case_id` exposes the same ID for browser and native UI
helpers. The function-scoped `visual` fixture binds that ID to its `VisualHarness`, while
the session-scoped Playwright driver remains shared. With `view`, `await view.capture()`
writes the default screenshot (`checkpoint=None`) for that case. Use readable parameter IDs.
App-owned fixtures call `visual.open()` without passing an ID. Outside pytest, construct
`VisualHarness(..., outputs=BoundTestOutputs(output_dir, case_id))` with an explicit case identity.

`VisualPage.outputs` is a `BoundTestOutputs` writer from `devinfra.testing.output_writer`,
bound to the page's output directory and case ID. Use `view.outputs.publish(...)` for
additional roles or checkpoints alongside `view.capture()`; both publish under the same
case identity. The writer is independent of pytest, so real-server pages can use it too.

`test_outputs.publish(role="diagnostics", suffix="json", data=json.dumps(details))`
accepts text (encoded as UTF-8) or bytes. Code that writes to a file handle can use
`with test_outputs.open(role="diagnostics", suffix="json") as output:` and write
text to `output`; pass `mode="wb"` for bytes. The handle writes to a temporary file and
publishes on successful exit. An exception publishes nothing, and a second write to
the same case, checkpoint and role fails. Use `publish_file(...)` for an existing
file.

`view.capture()` accepts an optional checkpoint name and caption. Use a distinct
checkpoint for each additional capture in one test. Pass a strict `Locator` to crop
one component, omit it to capture the viewport, or use `full_page=True`. Crops
retain the nearest-pixel rounding convention. `devtools_viewport` retains device-pixel
compatibility for existing galleries. Duplicate case/checkpoint/role claims fail.

`VisualHarness.open()` retains a standard Playwright trace for its bound case by default.
Custom deterministic-context fixtures can use `playwright_trace(context, outputs,
checkpoint)` with a `BoundTestOutputs` writer. Set `VISUAL_TRACE=0` to disable tracing. Set
`VISUAL_TRACE_CAPTURE_NAME` to the exact full `case_id` to retain only that case.
When a test opens multiple pages, give each a distinct `trace_checkpoint`. Trace files
are staged in temporary directories for every opening, and a duplicate case/checkpoint trace fails
without replacing the first record. If a failing test also cannot publish its trace,
the raw trace is copied to undeclared outputs and the exception note names it.

To trace one case, set `VISUAL_TRACE_CAPTURE_NAME` and filter the Bazel target:

```bash
bbr test //package:visual_test \
  --test_arg=-k --test_arg=test_details \
  --test_env=VISUAL_TRACE_CAPTURE_NAME='example/test_visual.py::test_details'
```

The trace is recorded under `test-outputs/` with role `trace`, including when the
case passes. It contains Playwright DOM snapshots and network activity; use it only
with synthetic visual cases. `VISUAL_TRACE` accepts `0` or `1` and defaults to `1`.

## One way in

Every review screenshot comes from a page of `deterministic_browser_context`
(`util/testing/frontend_visual/context.py`) published by `VisualPage.capture()`. Choose the entry by
where the page is served:

- Mounted by a bundled harness: `VisualHarness.open()` or the `view` fixture
  (`util/testing/frontend_visual/fixtures.py`).
- Served by a real server: `deterministic_browser_context(...)` plus
  `with VisualPage(page, ...) as view:`, entered before navigating. Exiting the context checks
  for uncaught page errors, including errors after the last screenshot. If another error is
  already propagating, the page error is attached to it as a note.
- Native UI (GNOME shell, ...): `publish_output()` (`devinfra/testing/output_writer.py`), as
  `gnome/testing/session.py` does.

Traps, each of which renders differently from one execution to the next or hides a failure:

- **`util.playwright`'s `browser`/`page` fixtures** carry only the container flags, not the font
  profile or raster flags. Never screenshot a review asset from one.
- **Raw `page.screenshot()` plus a hand-written manifest entry** skips the font, image and paint
  readiness checks and the page-error check. Diagnostic screenshots are fine; anything reviewers see
  goes through `capture()`.
- **A real-server page does not get the animation-pinning CSS** that inline harnesses inline. Pin it
  with `disable_animations_script()` as a context init script, or pass `animations="disabled"` to
  `capture()`; an unpinned spinner is captured at a random rotation.
- **`frozen_now_ms=None`** (live clock) is for pages whose replication runs on real time; any other
  date-dependent text needs a frozen instant.
- **Sleeping for a duration** instead of waiting for the state (`STYLE.md` § Waiting).

## Health and determinism

Each harness opening gets a fresh deterministic browser: pinned Chromium, font profile,
locale, timezone and frozen clock. Harness styles must pin animations before components
mount; inline pages include that CSS automatically. Capture waits for fonts, image decoding,
paint and the fixture network ledger, and checks uncaught page errors and escaped requests.
These are **not** application-readiness conditions: the test must assert its content first.

Capture then repaints the whole page (`repaint` in `page_capture.py`) before taking the
screenshot. Chromium's compositor can keep a layer's tiles rastered for the pixel alignment the
layer had when the tiles were made. A sticky heading changes alignment as it sticks and unsticks,
so without the repaint its text landed a device pixel off depending on scroll timing. A DevTools
capture also hides the text caret, as `page.screenshot()` does by default, because the caret blinks
on wall-clock time.

The launcher pins Chromium's native device scale to the context's emulated scale with
`--force-device-scale-factor`. Emulation alone changes the DOM's `devicePixelRatio` while
leaving the native widget at its default scale (1 on RBE). Chromium's [screenshot handler] then rescales the
compositor for each capture and restores it afterwards. This produced two SVG edge rasters
on an unchanged mobile page; waiting for fonts and paint frames did not eliminate it.
Pinning the native scale removes that screenshot-time scale transition. Fractional-scale
images can change once when adopting the pin, including font metrics and SVG edges.

[screenshot handler]: https://chromium.googlesource.com/chromium/src/+/151.0.7922.34/content/browser/devtools/protocol/page_handler.cc#1476

Capture does not click, scroll, or move the pointer.

Real-server tests may construct `VisualPage` before navigating an existing deterministic
page, with their own server setup and network policy. Diagnostic screenshots need not be
published as visual-review assets.

Screenshots, diagnostics, and traces go to Bazel undeclared outputs as test-output records. There are no
checked-in pixel baselines: behavior/render health are hard gates, while pixel changes
are reviewed through `devinfra/pr_visuals`. The collector compares structured case and checkpoint identities across runs.

Run browser tests on RBE through `bbr`, or use PR CI. Pytest selection/sharding and the
weekly `visual`-tagged determinism sweep continue to apply. Inspect both test results and
the PR visual comparison; a passing test is not proof of an unchanged image.

Check reproducibility by repeating the same test externally (for example,
`bbr test --runs_per_test=5 //study_casino:visual_test`) or using the weekly
visual determinism sweep, which compares artifacts across runs. Do not add a
second browser render and pixel-equality assertion to each screenshot test.
Repeated test success alone checks for flakes, not identical pixels; compare
artifacts when investigating image reproducibility.
