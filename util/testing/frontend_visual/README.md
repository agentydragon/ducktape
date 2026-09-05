# frontend_visual

Shared Puppeteer/Playwright infrastructure for per-scenario visual render-health
tests (see `visual-test-lib.mjs` for the JS/Puppeteer path used by
`study_casino/frontend`, `props/frontend`, and `airlock/frontend`, and
`frontend_visual.py` for the Python/Playwright path used by `study_casino/tests`
and `finance/augur`). `capture.mjs` holds the lower-level page-prep/capture
primitives (`prepareDeterministicPage`, `screenshotElement`, `waitForStable`) that
`visual-test-lib.mjs` and haku console's own multi-scene renderers
(`haku/console/frontend/screenshots/render.mjs`,
`haku/console/frontend/tool_rendering/screenshot/render.mjs`) build on — a
library, not a `main()`, so each caller keeps owning content-loading,
orchestration, and its own exit code.

There are no checked-in pixel baselines: these tests gate render health (the
harness loads, the scenario mounts, zero uncaught page errors) and publish the
rendered PNG for PR visual review instead — see
`devinfra/pr_visuals/plans/goldens_to_pr_visuals.md`.

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

## Waiting for a scene

`main()` takes `readySelectors`: the scene's own readiness conditions, waited for
before the capture. `waitForStable` (fonts applied, images decoded, a frame
painted) knows nothing about a scene's content, so anything that arrives after
mount — a mocked fetch's result, a lazily-mounted component — needs a selector
that exists only once it has arrived. A scene with nothing arriving after mount
passes none.

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
the target twice with fresh (non-cached) execution and diff BuildBuddy's
artifact content digests directly, without downloading anything:

```bash
bbr test --noremote_accept_cached --nocache_test_results //path/to:target   # run 1
bbr test --noremote_accept_cached --nocache_test_results //path/to:target   # run 2
bbapi artifact list <invocation-1> --json > /tmp/run1.json
bbapi artifact list <invocation-2> --json > /tmp/run2.json
# diff the "uri" field per asset name — identical content hashes to the same blob URI
```

If they differ, `launchDeterministicBrowser()` + `DISABLE_ANIMATIONS_CSS` (both in
`launcher.mjs`) close off rendering-level jitter (font rasterization, unguarded
CSS animations), but not a page that's still loading: `visual-test-lib.mjs` waits
with `waitUntil: "networkidle0"` for exactly this reason, rather than `"load"`,
which returns as soon as the initial HTML parses regardless of in-flight fetches.
