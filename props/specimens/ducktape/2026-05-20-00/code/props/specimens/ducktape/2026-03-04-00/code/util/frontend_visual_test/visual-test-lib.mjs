/**
 * Shared infrastructure for per-scenario visual regression tests.
 *
 * Each scenario has its own js_test target that imports this lib and calls
 * main(scenarioName). This gives Bazel proper per-scenario caching and
 * parallelism: a change to CoverageHeatmap's baseline only reruns that test.
 *
 * Uses file:// URLs to load the harness HTML directly — no HTTP server needed.
 * The harness bundle is IIFE format so it works without module CORS restrictions.
 *
 * To update a baseline: bazel run //path/to:visual_TestName -- --update
 * To inspect failures: check TEST_UNDECLARED_OUTPUTS_DIR for *-actual.png + *-diff.png.
 */

import puppeteer from "puppeteer";
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "fs";
import { PNG } from "pngjs";
import pixelmatch from "pixelmatch";
import { fileURLToPath } from "url";
import { dirname, join, resolve } from "path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Default baseline directory (adjacent to this lib file).
// Can be overridden per-test via the baselineDir parameter to main().
const DEFAULT_BASELINE_DIR = join(__dirname, "visual-regression.spec.ts-snapshots");

// Pixel diff tolerance (percentage of total pixels). See visual-regression.spec.js for rationale.
const PIXEL_DIFF_PERCENT = 2;

/**
 * Determine whether update mode is active and where to write baselines.
 *
 * Returns { updateMode, writeDir } where writeDir is the source-tree path
 * when updating, or null for comparison mode.
 */
function resolveUpdateMode() {
  const updateRequested = process.argv.slice(2).includes("--update") || process.env.UPDATE_BASELINES === "1";
  if (!updateRequested) return { updateMode: false, writeDir: null };

  const workspaceDir = process.env.BUILD_WORKSPACE_DIRECTORY;
  if (!workspaceDir) {
    console.error(
      "ERROR: --update requires BUILD_WORKSPACE_DIRECTORY (use 'bazel run', not 'bazel test').\n" +
        "Usage: bazel run //path/to:visual_TestName -- --update"
    );
    process.exit(1);
  }

  const baselineRelPath = process.env.BASELINE_WORKSPACE_PATH;
  if (!baselineRelPath) {
    console.error("ERROR: BASELINE_WORKSPACE_PATH not set. Is the visual_test() macro up to date?");
    process.exit(1);
  }

  return { updateMode: true, writeDir: join(workspaceDir, baselineRelPath) };
}

function compareBaseline(name, screenshot, outputDir, baselineDir, updateWriteDir) {
  if (!existsSync(outputDir)) mkdirSync(outputDir, { recursive: true });
  writeFileSync(join(outputDir, `${name}-actual.png`), screenshot);

  if (updateWriteDir) {
    if (!existsSync(updateWriteDir)) mkdirSync(updateWriteDir, { recursive: true });
    const dest = join(updateWriteDir, `${name}-chromium-linux.png`);
    writeFileSync(dest, screenshot);
    console.log(`  Updated baseline: ${dest}`);
    return { passed: true, updated: true };
  }

  const baselinePath = join(baselineDir, `${name}-chromium-linux.png`);
  if (!existsSync(baselinePath)) {
    if (!existsSync(baselineDir)) mkdirSync(baselineDir, { recursive: true });
    writeFileSync(baselinePath, screenshot);
    console.log(`  Creating baseline: ${name}`);
    return { passed: true, created: true };
  }

  const baseline = PNG.sync.read(readFileSync(baselinePath));
  const actual = PNG.sync.read(screenshot);

  if (actual.width !== baseline.width || actual.height !== baseline.height) {
    console.error(
      `  ✗ Dimensions differ: baseline=${baseline.width}x${baseline.height}, actual=${actual.width}x${actual.height}`
    );
    return { passed: false, reason: "dimensions" };
  }

  const diff = new PNG({ width: baseline.width, height: baseline.height });
  const numDiffPixels = pixelmatch(baseline.data, actual.data, diff.data, baseline.width, baseline.height, {
    threshold: 0.3,
  });

  const totalPixels = baseline.width * baseline.height;
  const diffPercent = (numDiffPixels / totalPixels) * 100;

  if (diffPercent > PIXEL_DIFF_PERCENT) {
    writeFileSync(join(outputDir, `${name}-diff.png`), PNG.sync.write(diff));
    console.error(`  ✗ ${numDiffPixels} pixels differ (${diffPercent.toFixed(1)}% > ${PIXEL_DIFF_PERCENT}% tolerance)`);
    return { passed: false };
  }

  if (numDiffPixels > 0) {
    console.log(`  ~ ${numDiffPixels} pixels differ (${diffPercent.toFixed(1)}% within ${PIXEL_DIFF_PERCENT}% tolerance)`);
  }
  return { passed: true };
}

/**
 * Run a single visual scenario and exit 0 (pass) or 1 (fail).
 * Called from each per-scenario test file.
 *
 * @param {string} scenarioName - Harness page name (e.g. "ListPage").
 * @param {string|null} callerUrl - import.meta.url of the calling test file (for baseline dir resolution).
 * @param {{ viewport?: { width: number, height: number }, baselineName?: string }} [options] - Optional overrides.
 *   baselineName overrides the filename stem for the baseline PNG (defaults to scenarioName).
 */
export async function main(scenarioName, callerUrl = null, options = {}) {
  const baselineName = options.baselineName || scenarioName;

  // Resolve baseline directory. Prefer BASELINE_WORKSPACE_PATH (set by the
  // visual_test() Bazel macro) so baselines resolve correctly even when the
  // test file lives in a subdirectory (e.g., tests/) separate from baselines/.
  let baselineDir;
  const baselineWsPath = process.env.BASELINE_WORKSPACE_PATH;
  if (baselineWsPath && callerUrl) {
    const callerDir = dirname(fileURLToPath(callerUrl));
    const pkgPath = dirname(baselineWsPath);
    const idx = callerDir.lastIndexOf(pkgPath);
    if (idx >= 0 && (idx === 0 || callerDir[idx - 1] === "/")) {
      baselineDir = join(callerDir.substring(0, idx), baselineWsPath);
    }
  }
  if (!baselineDir) {
    baselineDir = callerUrl
      ? join(dirname(fileURLToPath(callerUrl)), "baselines")
      : DEFAULT_BASELINE_DIR;
  }
  const harnessPath = process.env.HARNESS_PATH || join(__dirname, "harness/dist/harness.js");
  const distDir = dirname(harnessPath);
  const harnessDir = distDir.endsWith("/dist") ? dirname(distDir) : distDir;
  const outputDir = process.env.TEST_UNDECLARED_OUTPUTS_DIR || join(__dirname, "diffs");

  const { updateMode, writeDir } = resolveUpdateMode();

  const indexPath = resolve(join(harnessDir, "index.html"));
  if (!existsSync(indexPath)) {
    console.error(`Harness index.html not found in: ${harnessDir}`);
    process.exit(1);
  }

  const userDataDir = join(process.env.TEST_TMPDIR || process.cwd(), `chrome-user-data-${baselineName}`);
  mkdirSync(userDataDir, { recursive: true });

  const launchOptions = {
    headless: true,
    userDataDir,
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-dev-shm-usage",
      "--disable-gpu",
      "--single-process",
      "--allow-file-access-from-files",
      "--font-render-hinting=none",
      "--disable-font-subpixel-positioning",
      "--disable-lcd-text",
      "--force-color-profile=srgb",
      "--disable-accelerated-2d-canvas",
      "--disable-gpu-compositing",
      "--disable-software-rasterizer",
      "--disable-skia-runtime-opts",
      "--disable-partial-raster",
      "--disable-backing-store-limit",
      "--use-gl=swiftshader",
      "--force-device-scale-factor=1",
      "--disable-features=CalculateNativeWinOcclusion,VizDisplayCompositor",
      "--disable-accelerated-video-decode",
      "--disable-canvas-aa",
      "--disable-2d-canvas-clip-aa",
      "--disable-webgl",
      "--disable-webgl2",
      "--blink-settings=imageAnimationPolicy=noAnimation",
      "--disable-smooth-scrolling",
      "--disable-threaded-animation",
      "--disable-threaded-scrolling",
      "--disable-checker-imaging",
    ],
  };

  if (process.env.PUPPETEER_EXECUTABLE_PATH) {
    let execPath = process.env.PUPPETEER_EXECUTABLE_PATH;
    const playwrightExec = join(execPath, "chrome-linux", "headless_shell");
    if (existsSync(playwrightExec)) execPath = playwrightExec;
    console.log(`Using browser at: ${execPath}`);
    launchOptions.executablePath = execPath;
  }

  if (updateMode) {
    console.log(`Updating baseline for: ${baselineName}`);
  }

  const browser = await puppeteer.launch(launchOptions);
  let passed = false;

  try {
    const page = await browser.newPage();
    const viewport = { width: 1200, height: 800, deviceScaleFactor: 1, ...options.viewport };
    await page.setViewport(viewport);
    await page.emulateMediaFeatures([
      { name: "prefers-color-scheme", value: "light" },
      { name: "prefers-reduced-motion", value: "reduce" },
    ]);

    const harnessUrl = `file://${indexPath}`;

    // Verify Inter font loads
    await page.goto(harnessUrl, { waitUntil: "networkidle0" });
    const fontLoaded = await page.evaluate(() => document.fonts.check("16px Inter"));
    if (!fontLoaded) {
      console.error("FATAL: Inter font did not load");
      process.exit(1);
    }

    console.log(`Testing: ${baselineName} (page=${scenarioName})`);
    await page.goto(`${harnessUrl}?page=${scenarioName}`, { waitUntil: "networkidle0" });
    await page.waitForSelector("#app > *", { timeout: 5000 });
    await new Promise((r) => setTimeout(r, 200));

    const element = await page.$("#app");
    const screenshotData = await element.screenshot();
    const screenshot = Buffer.isBuffer(screenshotData) ? screenshotData : Buffer.from(screenshotData);

    const result = compareBaseline(baselineName, screenshot, outputDir, baselineDir, writeDir);
    if (result.updated) {
      console.log("  ✓ Baseline updated");
    } else if (result.created) {
      console.log("  ✓ Baseline created");
    } else if (result.passed) {
      console.log("  ✓ Passed");
    }
    passed = result.passed;
  } finally {
    await browser.close();
  }

  process.exit(passed ? 0 : 1);
}
