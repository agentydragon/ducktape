# Python visual tests

Visual tests are ordinary async pytest tests that load a fixture and call the shared
screenshot API, optionally driving Playwright interactions and asserting behavior. A
mount-and-capture test is useful on its own: published before/after images in GitHub PR
comments let reviewers see what a UI change looks like. Readiness checks help capture the
intended state; behavioral assertions are additional coverage, not a prerequisite.
There is no JSON/jq instruction interpreter and no browser-side test driver.

## Responsibilities

- **Tests** own fixture selection, readiness, actions, assertions, scrolling, pointer
  placement and screenshot checkpoints. Use normal functions and pytest parameterization.
  Fixture identifiers select data, not viewport, theme, readiness, or interactions. Reuse a
  fixture for multiple tests. Screenshot names identify outputs; do not dispatch behavior
  from their spelling or suffixes.
- **TS harnesses** mount production components and provide synthetic fixture data or fake
  services. A shared/generated fixture catalog may enumerate data; it must not contain
  selectors, clicks, waits, or capture instructions.
- **`VisualHarness` / `VisualPage`** (`util/testing/visual_capture.py`) own browser lifecycle,
  render-health checks, screenshot mechanics and review publication.
- **`py_visual_test`** owns Bazel runfiles, the Python entry point and the `visual` tag.

## Example

```python
import pytest
import pytest_bazel
from playwright.async_api import expect

from util.testing.visual_capture import VisualHarness

# gazelle:include_dep //util/testing:visual_fixtures
pytest_plugins = ("util.testing.visual_fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_details(visual: VisualHarness) -> None:
    async with visual.open("example") as view:
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

`visual.open()` accepts browser geometry (`util/testing/viewports.py`), color scheme,
query parameters and an optional frozen instant. Inline harnesses use `window_globals`
instead of a query. The macro's `page_url` gives an inline document an origin for
storage; `served_documents` supplies mock iframe documents.

The shared pytest plugin also provides a per-test `view` yield fixture, with ordinary
`viewport` and `color_scheme` fixtures that tests can override or parametrize. It opens
an unnamed harness and closes its isolated browser context after the test. App-specific
fixtures can depend on it to prepare reusable state without hiding test interactions.

With `view`, `await view.capture()` derives the PNG name from the pytest function and
parameter IDs. Use readable parameter IDs; do not wire a parallel `image_name` column
just to identify each case. The `capture_name` fixture exposes this same identity to
harnesses that need their own page-opening fixture. Pass it as `visual.open(capture_name=...)`
to use unnamed capture there too. It never selects fixture data or app behavior.

`view.capture()` also accepts an explicit unique output name and optional caption;
use explicit names for multiple checkpoints in one test. Pass a strict
`Locator` to crop one component, omit it to capture the viewport, or use
`full_page=True`. Crops retain the nearest-pixel rounding convention.
`devtools_viewport` retains device-pixel compatibility for existing galleries.
Duplicate output names within an execution fail instead of overwriting an image.

## Health and determinism

Each harness opening gets a fresh deterministic browser: pinned Chromium, font profile,
locale, timezone and frozen clock. Harness styles must pin animations before components
mount; inline pages include that CSS automatically. Capture waits for fonts, image decoding,
paint and the fixture network ledger, and checks uncaught page errors and escaped requests.
These are **not** application-readiness conditions: the test must assert its content first.
Capture does not click, scroll, or move the pointer.

Real-server tests may construct `VisualPage` before navigating an existing deterministic
page, with their own server setup and network policy. Diagnostic screenshots need not be
published as visual-review assets.

Screenshots and `visual-review.json` go to Bazel undeclared outputs. There are no
checked-in pixel baselines: behavior/render health are hard gates, while pixel changes
are reviewed through `devinfra/pr_visuals`. Preserve target names and asset filenames
when migrating so the publisher can match existing baselines.

Run browser tests on RBE through `bbr`, or use PR CI. Pytest selection/sharding and the
weekly `visual`-tagged determinism sweep continue to apply. Inspect both test results and
the PR visual comparison; a passing test is not proof of an unchanged image.

Check reproducibility by repeating the same test externally (for example,
`bbr test --runs_per_test=5 //study_casino:visual_test`) or using the weekly
visual determinism sweep, which compares artifacts across runs. Do not add a
second browser render and pixel-equality assertion to each screenshot test.
Repeated test success alone checks for flakes, not identical pixels; compare
artifacts when investigating image reproducibility.
