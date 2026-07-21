# haku console screenshot harnesses: flaky sub-pixel PR-visual diffs

Status: **resolved** — both custom Puppeteer drivers now launch through
`launchDeterministicBrowser`, freeze the wall clock, and inject a hard
animation/transition kill into their own page HTML.

`//haku/console/frontend:screenshots` and `tool_rendering/<server>:previews` targets
occasionally showed up in PR visual-review comments as "modified" against an unchanged
baseline, with a changed fraction that rounds to "0.0%" — e.g.
github.com/agentydragon/ducktape/pull/3473's `chrome-sync-ok` dark/light entries, each a
before/after/diff table with nothing visibly different. Investigation notes below; kept
for the next time this class of noise shows up somewhere that hasn't adopted this
harness's wiring yet.

## Confirmed: genuine rendering nondeterminism, not a stale baseline or a real UI change

Ran `//haku/console/frontend:screenshots` twice in a row against the exact same commit
(`bbr test --noremote_accept_cached --nocache_test_results` to force fresh execution both
times, comparing BuildBuddy artifact content digests directly — no S3 PR-visuals baseline
involved, so version skew between baseline and candidate commits is not a factor):

| asset                                       | run 1 vs run 2                             |
| ------------------------------------------- | ------------------------------------------ |
| `chrome-dark.png` / `chrome-light.png`      | differ                                     |
| `chrome-sync-error-dark.png` / `-light.png` | differ                                     |
| `chrome-sync-ok-dark.png` / `-light.png`    | differ (or occasionally match — see below) |
| `history-{dark,light}.png`                  | always identical                           |
| `settings-{dark,light}.png`                 | always identical                           |

Pixel-diffing two `chrome-sync-ok-dark.png` runs directly: ~100-250 pixels differ out of
720,000, every one inside one small bounding box (the fixed top-right toolbar), each off
by ±1-2 in a single RGB channel. `history`/`settings` never render that toolbar and never
differ. Which exact asset shows a diff varies run to run (not a fixed pattern) — consistent
with genuine timing-driven jitter, not a deterministic content or version difference.

Ruled out "baseline built with a different Chromium/Playwright pin": no `MODULE.bazel`
change to the Playwright pin exists between the PR's fallback-baseline commit and its base.

## First hypothesis (partially right, not sufficient alone): missing deterministic-launch flags

`util/testing/chromium-flags.json`'s `deterministicExtra` bundle (23 flags: font
hinting/subpixel positioning, LCD text, Skia runtime-dispatched SIMD opts, swiftshader,
partial raster, …) exists to pin exactly this class of rasterization jitter, exposed as
`launchDeterministicBrowser()` in `util/testing/frontend_visual/launcher.mjs`.
`visual-test-lib.mjs` (used by `props/frontend`, `x/study_casino/frontend`, `airlock/frontend`)
already launches through it; both haku console renderers instead called the plain
`launchBrowser()` with only one flag manually copied over (`--force-color-profile=srgb`).

Switching both to `launchDeterministicBrowser()` and freezing the clock via
`frozenClockScript` (`screenshots/harness.tsx`'s `ShellChromeScene` feeds a live
`Date.now()` into `sampleRecentToolCalls` — a second, independent latent nondeterminism
source, worth fixing regardless) did **not** eliminate the jitter: re-ran twice again,
still 5/9 PNGs differed, same bounding box, same ±1-2 magnitude. The flag bundle controls
how a given static frame rasterizes deterministically; it does nothing for a page whose
content is still actively animating at the moment of capture.

## Actual root cause: an unguarded Mantine CSS animation, not raster jitter

`shell_chrome.tsx` renders three `<Indicator processing>` elements in the toolbar (two
green "live" dots for geolocation/screenshot sharing, one red approvals-count badge) —
exactly the region every diff landed on, and exactly the three scenes affected
(`chrome`, `chrome-sync-ok`, `chrome-sync-error` all render `ShellChrome`; `history` and
`settings` don't and never differed).

Extracted the real shipped CSS from the pinned `@mantine/core@7.17.8` npm tarball
(`registry.npmjs.org` — `unpkg.com` is blocked by the environment's egress proxy):

```css
.m_760d1fb1[data-processing]::before {
  animation: m_885901b1 1000ms linear infinite;
}
@keyframes m_885901b1 {
  0% {
    opacity: 0.6;
    transform: scale(0);
  }
  100% {
    opacity: 0;
    transform: scale(2.8);
  }
}
```

No `@media (prefers-reduced-motion)` guard anywhere in the stylesheet around this rule.
`render.mjs` already calls `page.emulateMediaFeatures([{ name: "prefers-reduced-motion",
value: "reduce" }])`, but that only helps CSS that actually checks the media feature —
Mantine's `Indicator processing` ping doesn't, so it keeps running on the compositor's own
clock regardless. Whatever phase of the 1s loop is in flight at the exact screenshot
moment depends on real scheduling jitter between runs (page creation, `setContent`,
`waitForSelector`, the fixed 700ms settle, click delays) — a fraction of a percent either
way shows up as a differently-sized/opacity ring behind the dot or badge, i.e. a few
anti-aliased pixels off by ±1-2.

## Fix

Added `DISABLE_ANIMATIONS_CSS` to `launcher.mjs` (paired with `frozenClockScript` as a
shared, general-purpose determinism helper — not haku-console-specific, since any consumer
relying on `prefers-reduced-motion` alone has the same latent exposure the moment a
component doesn't respect it):

```css
*,
*::before,
*::after {
  animation-play-state: paused !important;
  transition: none !important;
}
```

Concatenated into both render.mjs's own inlined `<style>` tag (present before any content
mounts, so `data-processing` elements are created already paused at their first frame —
applying this after the fact would just freeze whichever frame the animation had already
reached). Re-ran twice more: all 10 PNGs byte-identical both times. Also re-ran
`tool_rendering/hostexec:previews` once to confirm the second renderer still passes and
renders correctly (spot-checked a card screenshot).

`visual-test-lib.mjs` was **not** changed — no evidence its consumers hit this (none
currently render an unconditionally-animating `Indicator`-style component), and it's
shared infra for three other frontends. Worth revisiting if the same "0.0% changed" symptom
ever shows up for `props/frontend`, `x/study_casino/frontend`, or `airlock/frontend`.
