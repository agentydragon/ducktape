# frontend_visual

Shared Puppeteer/Playwright infrastructure for visual render-health tests (see
`visual-test-lib.mjs` for the JS/Puppeteer path used by `agentplane/app/frontend`, and
`frontend_visual.py` for the Python/Playwright path used by `study_casino`,
`study_casino/frontend`, `finance/augur`, `airlock/frontend`, `aiquota/frontend`,
`props/frontend` and `devinfra/claude/session_export/frontend`).
Browser tests are moving to the Python path, lane by lane — see [The Python sweep](#the-python-sweep).
`capture.mjs` holds the lower-level page-prep/capture
primitives (`prepareDeterministicPage`, `screenshotElement`, `waitForStable`) that
`visual-test-lib.mjs` and haku console's own multi-scene renderers
(`haku/console/frontend/screenshots/render.mjs`,
`haku/console/frontend/tool_rendering/screenshot/render.mjs`) build on — a
library, not a `main()`, so each caller keeps owning content-loading,
orchestration, and its own exit code.

Both stacks drive one browser: the Chrome for Testing headless shell `@chrome_headless_shell`
(MODULE.bazel). Python tests find its executable in the runfiles of `//util/testing:frontend_visual`;
JS tests are handed its path as `CHROMIUM_HEADLESS_SHELL`.
Its version is pinned to the Chromium of the `playwright==1.62.0` driver in `pyproject.toml`;
MODULE.bazel says how to bump the two together.

There are no checked-in pixel baselines: these tests gate render health (the
harness loads, the scenario mounts, zero uncaught page errors) and publish the
rendered PNG for PR visual review instead — see
`devinfra/pr_visuals/plans/goldens_to_pr_visuals.md`.

## One target per scenario, or one target for all of them

`visual-test-lib.mjs` offers two entry points over the same capture path, and a package picks by
where it wants its scenario list to live.

- **`main(name, options)`** — one `js_test` per scenario, each with its own entry-point `.mjs`.
  Scenario names are then in BUILD as well as in the harness. No package works this way now.
- **`runScenarios(table, {title})`** — one `js_test` over a whole table, split with `shard_count`.
  The list lives only in the table; BUILD carries a shard count, which needs no edit when the
  table grows. One browser serves every scenario in a shard, and a failure is recorded and the
  sweep continues, so a run enumerates every broken scene rather than stopping at the first.
  `agentplane/app/frontend` works this way.

Under `runScenarios`, `--test_filter=<scenario>` (Bazel's `TESTBRIDGE_TEST_ONLY`) addresses a
single scenario — the substitute for a per-scenario target name. Filtering happens before
sharding, so the match runs wherever it lands and the other shards pass on nothing; a filter
matching no scenario fails rather than passing vacuously.

## The Python sweep

`py_visual_test` (`py_visual_test.bzl`) is `visual_test` on Playwright and pytest, and is where new
lanes go. A package supplies its harness page and a `scenarios.json`; the test is
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
)
```

Same contract as the sections below — `<outputName>-actual.png` and `visual-review.json`, ready
selectors, the request fence, the fetch ledger, zero uncaught page errors — with these deviations:

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
- **Selectors are Playwright's.** Puppeteer's `::-p-text(...)` does not exist; `readySelectors` wait for
  presence, as before.
- **The target is not `visual` if the harness lives in a `visual/` directory.** A `py_test`'s
  executable is `<package>/<name>`, which collides with that directory's outputs (`js_test` hides its
  executable in `<name>_/`). Keep the target name and call the directory `harness/`.

## Screenshot target: element, not viewport

`visual-test-lib.mjs`'s `main()` takes a required `element` CSS selector — there
is no default, so every scenario states explicitly which of the two cases it is:

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
pass the asset plus `font_family` to `visual_test` or `py_visual_test` when using the shared macro. This keeps named
typography in the product's normal CSS while keeping generic-family determinism independent of
the page cascade.

## Waiting for a scene

Every scenario takes `readySelectors`: the scene's own readiness conditions, waited for
before the capture. `waitForStable` (fonts applied, images decoded, a frame
painted) knows nothing about a scene's content, so anything that arrives after
mount — a mocked fetch's result, a lazily-mounted component — needs a selector
that exists only once it has arrived. A scene with nothing arriving after mount
passes none. A scene whose page throws while the mount wait or one of these is
pending fails with that error (`waitForSelectorUnlessPageError`), not with the
wait's timeout.

There is no delay option to fall back on: a fixed wait is too short on a loaded
runner and pure dead time on every run that did not need it, and it hides what is
being awaited (STYLE.md § Waiting). A scene with nothing to wait on is an app to
fix — give the view a `data-` attribute or class it sets when it has its data —
not a timer to tune.

## Wait bounds

Every wait takes `WAIT_TIMEOUT_MS` from `capture.mjs` — both navigations, the
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

If they differ, `launchDeterministicBrowser()` + `DISABLE_ANIMATIONS_CSS` (both in
`launcher.mjs`) close off rendering-level jitter (pinned browser font preferences,
font rasterization, unguarded CSS animations), but not a page that's still loading: `visual-test-lib.mjs` waits
with `waitUntil: "networkidle0"` for exactly this reason, rather than `"load"`,
which returns as soon as the initial HTML parses regardless of in-flight fetches.
