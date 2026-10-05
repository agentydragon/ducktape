# frontend_visual

Shared infrastructure for visual render-health tests. Every lane runs on the Python/Playwright path
(`py_visual_test`; `frontend_visual.py`, `visual_sweep.py`), except haku console's own multi-scene
renderer (`haku/console/frontend/screenshots/render.mjs`), which still runs on Puppeteer and builds on
`capture.mjs`'s page-prep/capture primitives (`prepareDeterministicPage`, `screenshotElement`,
`waitForStable`) and `launcher.mjs` -- a library, not a `main()`, so the caller keeps owning
content-loading, orchestration, and its own exit code. See [The Python sweep](#the-python-sweep).

Both stacks drive one browser: the Chrome for Testing headless shell `@chrome_headless_shell`
(MODULE.bazel). Python tests find its executable in the runfiles of `//util/testing:frontend_visual`;
JS tests are handed its path as `CHROMIUM_HEADLESS_SHELL`.
Its version is pinned to the Chromium of the `playwright==1.62.0` driver in `pyproject.toml`;
MODULE.bazel says how to bump the two together.

There are no checked-in pixel baselines: these tests gate render health (the
harness loads, the scenario mounts, zero uncaught page errors) and publish the
rendered PNG for PR visual review instead — see
`devinfra/pr_visuals/plans/goldens_to_pr_visuals.md`.

## The Python sweep

`py_visual_test` (`py_visual_test.bzl`) sweeps a table of scenarios on Playwright and pytest. A package supplies its harness page and a `scenarios.json`; the test is
`//util/testing:visual_sweep`, so there is no Python in the package.

```python
py_visual_test(
    name = "visual",
    size = "small",
    shard_count = 2,  # optional
    assets = ["harness/index.html", ":visual_harness_css"],
    harness = ":visual_harness_js",
    scenarios = "harness/scenarios.json",
    title = "Airlock",
    # fonts = ":app_fonts", font_family = "Outfit",  # an app-owned named font, asserted rendered
    # devtools_viewport = True,  # see below
)
```

Each scenario is published as `<outputName>-actual.png` and an entry in `visual-review.json`, gated on
its ready selectors, the request fence, the fetch ledger and zero uncaught page errors. Specifics:

- **The table is `scenarios.json`**, pure data. The TypeScript harness imports it
  (`import SCENARIOS from "./scenarios.json"`) and the sweep reads it, so no scenario is listed twice;
  fields only the harness reads sit in the same object. The sweep's fields are in
  `util/testing/visual_scenarios.py`; `viewport` also takes `deviceScaleFactor` and `hasTouch`, and a
  scenario can name a `hover` or `tap` selector. The harness is loaded at `?page=<scenario>`; a
  scenario's `query` replaces that query string, for a harness keyed otherwise or one scene shown in
  several scenarios. `label` is the caption in PR visual review (the output name if unset), and the
  PNG is `<outputName>-actual.png` unless the macro's `output_suffix` says otherwise. A table whose
  rows derive from data the harness already consumes is generated from it at build time, not copied.
- **A scenario is a pytest test**, `test_scenario[<name>]`, so Bazel's test report has one case per
  scenario. `--test_filter` is pytest's `-k`: a case-insensitive match that also sees the file name.
  Shards deal scenarios out by position, filter first (`util/testing/sharding.py`), and a filter that
  matches nothing fails.
- **A named font must be declared as well as loaded.** With `fonts` and `font_family`, each scenario
  asserts that an `@font-face` declares the family and that it loaded. `document.fonts.check` alone
  (the Puppeteer sweep's assertion) is true for a family nothing declares, so a stylesheet that never
  arrived passes it and the page renders in the fallback font. The assertion is made of the mounted,
  painted scene: a face loads only once laid-out text uses it, which on a loaded worker is after
  the navigation's network idle.
- **A fresh browser per scenario**, not one per shard. Launch and close cost about 0.1s on the RBE
  worker, and what one scenario renders cannot then depend on the scenarios that ran before it.
- **An element is captured to the nearest pixel**, as Puppeteer does, not outward as Playwright's own
  element screenshot would (a `#app` 1630.4px tall publishes 1630 rows, not 1631), so a migrated lane's
  images keep their sizes. An element taller than the viewport is captured whole.
- **A page can be assembled in memory** instead of being a `file://` `index.html` beside the bundle:
  `py_visual_test(inline_page = True, stylesheets = [...], base_href = ...)` inlines the bundle, the
  stylesheets and `DISABLE_ANIMATIONS_CSS` into a document loaded with `set_content`. Such a page has no
  URL query, so a scenario's `windowGlobals` assign the `window` values that tell the harness which scene
  it is, and the request fence allows nothing at all. `harness` may then be an esbuild `output_dir`.
  `haku/console/frontend/tool_rendering/screenshot` is the example, its table generated at build time from
  each server's fixtures.
- **`devtools_viewport = True` emulates and captures the viewport the way Puppeteer did**
  (`DevtoolsViewport` in `page_capture.py`: `Emulation.setDeviceMetricsOverride`, and an unclipped
  `Page.captureScreenshot` for a `captureViewport` scenario). Playwright's own viewport rasterizes a few
  pixels differently at some device scale factors (identical at 1 and 2; 1.5, 2.625 and 3 differ), so a
  lane with such a scale that must stay byte-identical to its Puppeteer sweep turns it on. Off by default.
- **Selectors are Playwright's.** Puppeteer's `::-p-text(...)` does not exist; `readySelectors` wait for
  presence, as before.
- **The target is not `visual` if the harness lives in a `visual/` directory.** A `py_test`'s
  executable is `<package>/<name>`, which collides with that directory's outputs (`js_test` hides its
  executable in `<name>_/`). Keep the target name and call the directory `harness/`.

## Screenshot target: element, not viewport

A scenario's `element` is a required CSS selector — there is no default, so every
scenario states explicitly which of the two cases it is:

- **`element: "#app"`** — the scenario is a genuine full page or full app (nav,
  header, the works). A full-page/viewport-shaped screenshot is the correct
  capture here.
- **`element: "#shot"`** (by convention) — the scenario's real subject is a
  single component (a toast, a modal, a chart, a card) pulled out of its normal
  page for isolated testing. The harness must mount it inside a wrapper with
  that id, sized to the component's own content, and the test screenshots only
  that wrapper.

Getting this wrong looks like: wrapping a small component in an artificial
`minHeight: "100vh"` box and hand-picking a viewport size to roughly match it,
then screenshotting the whole page. That produces a PNG that's mostly empty
background, and the "roughly match it" viewport is a guess that goes stale the
first time the component's real size changes. See
<https://github.com/agentydragon/ducktape/pull/3343> for the original instance
of this bug and fix.

If the single component has a real production container that owns its width
(a dashboard grid cell, a fixed-width panel column), render it inside that
actual container/class in the harness — not a synthetic hardcoded width — so
the screenshot tracks the true CSS instead of a number that can drift from it.

## Font ownership

Generic CSS families (`serif`, `sans-serif`, `monospace`, and `system-ui`) resolve through
Chromium's seeded profile preferences and deterministic font flags. The browser environment
owns that mapping; visual harnesses must not inject a blanket `font-family` rule.

An application that intentionally uses a named font owns its font asset and `@font-face` rule.
Fetch a pinned external asset through Bazel when practical, bundle it with the application, and
pass the asset plus `font_family` to `py_visual_test`. This keeps named
typography in the product's normal CSS while keeping generic-family determinism independent of
the page cascade.

## Waiting for a scene

Every scenario takes `readySelectors`: the scene's own readiness conditions, waited for
before the capture. `wait_for_stable` (fonts applied, images decoded, a frame
painted) knows nothing about a scene's content, so anything that arrives after
mount — a mocked fetch's result, a lazily-mounted component — needs a selector
that exists only once it has arrived. A scene with nothing arriving after mount
passes none. A scene whose page throws while the mount wait or one of these is
pending fails with that error (`PageErrors.wait_for`), not with the
wait's timeout.

There is no delay option to fall back on: a fixed wait is too short on a loaded
runner and pure dead time on every run that did not need it, and it hides what is
being awaited (STYLE.md § Waiting). A scene with nothing to wait on is an app to
fix — give the view a `data-` attribute or class it sets when it has its data —
not a timer to tune.

## Wait bounds

Every wait takes `WAIT_TIMEOUT_MS` from `page_capture.py` — both navigations, the
mount wait, `assertNetworkSettled`, and any condition a scenario adds. One bound
in one place; why that number is at its declaration.

Don't put a literal next to a `waitFor*` call. The 5s that used to sit on the
mount wait was already ~60x the slowest healthy mount, and a loaded RBE worker
still outran it — reporting an arbitrary elapsed time rather than that the page
never mounted.

## Verifying determinism

A harness "passing" only proves it rendered — it says nothing about whether two
runs of the identical commit produce identical pixels. A scene that's still
animating or still waiting on a mocked async fetch at the moment of capture
produces exactly this: it looks the same to a human but differs by a few pixels
between runs, showing up as a misleadingly-labeled "X% changed" diff in PR visual
review. See <https://github.com/agentydragon/ducktape/pull/3478> and
<https://github.com/agentydragon/ducktape/pull/3481> for three real instances
(an unguarded Mantine CSS animation, and two mocked-fetch races against a fixed
delay).

To check a harness is actually deterministic, don't just eyeball the PNGs — run
it repeatedly with fresh (non-cached) execution and compare BuildBuddy's artifact
content digests, which needs nothing downloaded:

```bash
bb run //devinfra/pr_visuals:determinism_bin -- --runs 5 //path/to:target
```

Two runs is the minimum and rarely enough: all three instances above are races
that fire intermittently, and one that fires one time in five looks perfectly
stable across a pair. The same sweep runs weekly over every visual target
(`.github/workflows/visual-determinism.yml`) and can be dispatched on demand.

If they differ, `deterministic_browser_context` (`frontend_visual.py`) has already closed off
rendering-level jitter (pinned browser font preferences, font rasterization, the frozen clock), but not
a page that's still loading: the sweep navigates with `wait_until="networkidle"` for exactly this reason,
rather than `"load"`, which returns as soon as the initial HTML parses regardless of in-flight fetches.
