# Visual Regression Knob Ablation Results

Exhaustive test of each rendering knob in the visual regression test suite.
Each knob was individually **removed** and the resulting screenshot compared
pixel-by-pixel against the all-knobs-on baseline.

Two representative scenarios: `DefinitionDetail` (text-heavy, tables) and
`DistributionChartRecall` (chart rendering, historically worst diff).

Comparison threshold: pixelmatch 0.1 (stricter than the production 0.3).

## Test environments

| Label  | Where                            | BuildBuddy invocation                  |
| ------ | -------------------------------- | -------------------------------------- |
| local  | gVisor sandbox (Claude Code web) | `ac30de5f-f485-4bc9-b4c5-0ff6362593e3` |
| remote | BuildBuddy RBE worker            | `6a218389-a91b-4772-b8d5-7976f63b2eba` |

## Combined Results

| #   | Knob                                                | Category    | Local DefinitionDetail | Local DistChart     | RBE DefinitionDetail | RBE DistChart       |
| --- | --------------------------------------------------- | ----------- | ---------------------- | ------------------- | -------------------- | ------------------- |
| 1   | `--disable-gpu`                                     | chrome_flag | ERROR (crash)          | ERROR (crash)       | ERROR (timeout)      | ERROR (timeout)     |
| 2   | **`--font-render-hinting=none`**                    | chrome_flag | IDENTICAL              | **701px (0.226%)**  | IDENTICAL            | **362px (0.117%)**  |
| 3   | `--disable-font-subpixel-positioning`               | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 4   | **`--disable-lcd-text`**                            | chrome_flag | **3776px (0.597%)**    | **178px (0.057%)**  | **3763px (0.595%)**  | **202px (0.065%)**  |
| 5   | `--force-color-profile=srgb`                        | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 6   | `--disable-accelerated-2d-canvas`                   | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 7   | `--disable-gpu-compositing`                         | chrome_flag | ERROR (hang)           | ERROR (hang)        | IDENTICAL            | IDENTICAL           |
| 8   | `--disable-software-rasterizer`                     | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 9   | `--disable-skia-runtime-opts`                       | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 10  | `--disable-partial-raster`                          | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 11  | `--disable-backing-store-limit`                     | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 12  | `--use-gl=swiftshader`                              | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 13  | `--force-device-scale-factor=1`                     | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 14  | `--disable-features=...`                            | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 15  | `--disable-accelerated-video-decode`                | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 16  | `--disable-canvas-aa`                               | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 17  | `--disable-2d-canvas-clip-aa`                       | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 18  | `--disable-webgl`                                   | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 19  | `--disable-webgl2`                                  | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 20  | `--blink-settings=imageAnimationPolicy=noAnimation` | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 21  | `--disable-smooth-scrolling`                        | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 22  | `--disable-threaded-animation`                      | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 23  | `--disable-threaded-scrolling`                      | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 24  | `--disable-checker-imaging`                         | chrome_flag | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 25  | CSS: `-webkit-font-smoothing: none`                 | css         | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 26  | CSS: `text-rendering: geometricPrecision`           | css         | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 27  | CSS: animation/transition 0s                        | css         | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 28  | **CSS: Force Inter font family**                    | css         | **SIZE MISMATCH**      | **1124px (0.363%)** | **SIZE MISMATCH**    | **1162px (0.375%)** |
| 29  | Env: `FONTCONFIG_FILE`                              | env         | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 30  | Env: `FREETYPE_PROPERTIES`                          | env         | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 31  | Media: `prefers-color-scheme=light`                 | media       | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 32  | Media: `prefers-reduced-motion=reduce`              | media       | IDENTICAL              | IDENTICAL           | IDENTICAL            | IDENTICAL           |
| 33  | **Viewport: `deviceScaleFactor=1`**                 | viewport    | **SIZE MISMATCH**      | **SIZE MISMATCH**   | **SIZE MISMATCH**    | **SIZE MISMATCH**   |

## Summary: Which Knobs Actually Matter?

### Knobs that affect rendering (KEEP)

| Knob                            | Impact                   | Notes                                                                                          |
| ------------------------------- | ------------------------ | ---------------------------------------------------------------------------------------------- |
| `--disable-lcd-text`            | ~0.6% text, ~0.06% chart | **Largest single-flag impact.** LCD text uses sub-pixel rendering that varies across displays. |
| `--font-render-hinting=none`    | 0.1-0.2% on charts       | Affects chart text rendering. No effect on text-heavy pages.                                   |
| CSS: Force Inter font family    | SIZE MISMATCH + ~0.4%    | **Critical.** Without the hermetic font, system fonts change layout dimensions entirely.       |
| Viewport: `deviceScaleFactor=1` | SIZE MISMATCH            | **Critical.** Different scale factor = completely different pixel dimensions.                  |

### Knobs that crash/hang Chrome (KEEP for stability)

| Knob                        | Behavior                                                           |
| --------------------------- | ------------------------------------------------------------------ |
| `--disable-gpu`             | Required for headless_shell on gVisor. Removing it crashes Chrome. |
| `--disable-gpu-compositing` | Hangs locally (gVisor), harmless on RBE (native Linux kernel).     |

### Knobs with NO rendering effect (candidates for removal)

**20 of 33 knobs produce IDENTICAL output when removed**, both locally and on RBE:

| Chrome flags (no effect)                            | CSS/Env/Media (no effect)                 |
| --------------------------------------------------- | ----------------------------------------- |
| `--disable-font-subpixel-positioning`               | CSS: `-webkit-font-smoothing: none`       |
| `--force-color-profile=srgb`                        | CSS: `text-rendering: geometricPrecision` |
| `--disable-accelerated-2d-canvas`                   | CSS: animation/transition 0s              |
| `--disable-software-rasterizer`                     | Env: `FONTCONFIG_FILE`                    |
| `--disable-skia-runtime-opts`                       | Env: `FREETYPE_PROPERTIES`                |
| `--disable-partial-raster`                          | Media: `prefers-color-scheme=light`       |
| `--disable-backing-store-limit`                     | Media: `prefers-reduced-motion=reduce`    |
| `--use-gl=swiftshader`                              |                                           |
| `--force-device-scale-factor=1`                     |                                           |
| `--disable-features=...`                            |                                           |
| `--disable-accelerated-video-decode`                |                                           |
| `--disable-canvas-aa`                               |                                           |
| `--disable-2d-canvas-clip-aa`                       |                                           |
| `--disable-webgl`                                   |                                           |
| `--disable-webgl2`                                  |                                           |
| `--blink-settings=imageAnimationPolicy=noAnimation` |                                           |
| `--disable-smooth-scrolling`                        |                                           |
| `--disable-threaded-animation`                      |                                           |
| `--disable-threaded-scrolling`                      |                                           |
| `--disable-checker-imaging`                         |                                           |

### Infrastructure flags (not rendering-related, skip ablation)

These were skipped because they're required to run in sandboxed environments:

- `--no-sandbox` — required for headless in containers
- `--disable-setuid-sandbox` — required for headless in containers
- `--disable-dev-shm-usage` — prevents `/dev/shm` size issues
- `--single-process` — required for gVisor/container stability

## Interpretation

Out of **33 knobs** tested:

- **4 actually affect rendering** (12%): `--disable-lcd-text`, `--font-render-hinting=none`, hermetic font CSS, viewport scale factor
- **2 are required for stability** (6%): `--disable-gpu`, `--disable-gpu-compositing`
- **4 are infrastructure** (12%): sandbox/process flags
- **23 have zero measured effect** (70%)

The visual test could be significantly simplified. The essential rendering knobs are:

1. `--disable-lcd-text` (largest impact — sub-pixel rendering)
2. `--font-render-hinting=none` (chart text consistency)
3. Hermetic Inter font (prevents system font variation)
4. `deviceScaleFactor=1` (pixel-level consistency)
5. `--disable-gpu` + `--disable-gpu-compositing` (stability, not rendering)

The other 20+ flags are defensive (they _could_ matter in other rendering scenarios
not covered by these test pages) but have zero measured effect on the current test suite.

## Caveats

- Tested 2 of 8 scenarios. Other scenarios (FileViewerAnnotated, CoverageHeatmap, etc.)
  might exercise code paths where currently-inert flags become relevant.
- "IDENTICAL" means zero pixel difference at pixelmatch threshold 0.1 — very strict.
- The `--disable-gpu` flag crash may be specific to gVisor + headless_shell. On native
  Linux it might just toggle software rendering.
- CSS animation disabling and media features showed no effect here because the test
  harness uses static mock data with no animations. They would matter for live UI testing.
