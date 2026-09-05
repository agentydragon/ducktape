/**
 * Shared infrastructure for per-scenario visual render-health tests.
 *
 * Each scenario has its own js_test target that imports this lib and calls
 * main(scenarioName). This gives Bazel proper per-scenario caching and
 * parallelism: a change to CoverageHeatmap's harness deps only reruns that test.
 *
 * Uses file:// URLs to load the harness HTML directly — no HTTP server needed.
 * The harness bundle is IIFE format so it works without module CORS restrictions.
 *
 * There are no checked-in pixel baselines: the test gates render health
 * (harness loads, fonts load, the scenario mounts, zero uncaught page errors)
 * and writes `<name>-actual.png` plus a visual-review manifest to undeclared
 * outputs. Pixel changes are reviewed on the PR's visual-review page
 * (devinfra/pr_visuals/publisher.py) instead of gating CI — see
 * devinfra/pr_visuals/plans/goldens_to_pr_visuals.md.
 */

// `document` is the browser page's, referenced inside page.evaluate callbacks (which run in the
// headless page, not Node) — declare it so this Node script lints under the .mjs node-globals block.
/* global document */

import { existsSync, mkdirSync, writeFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join, resolve } from "path";

import {
  abortUnexpectedRequests,
  assertNetworkSettled,
  prepareDeterministicPage,
  screenshotElement,
  waitForStable,
  WAIT_TIMEOUT_MS,
} from "./capture.mjs";
import { launchDeterministicBrowser } from "./launcher.mjs";
import { upsertVisualReviewAsset } from "./visual-review-manifest.mjs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

/**
 * Run a single visual scenario and exit 0 (pass) or 1 (fail).
 * Called from each per-scenario test file.
 *
 * @param {string} scenarioName - Harness page name (e.g. "ListPage").
 * @param {{ element: string, viewport?: { width: number, height: number }, outputName?: string, colorScheme?: 'light' | 'dark', readySelectors?: string[] }} options - Overrides.
 *   element is the CSS selector to screenshot. Required — there is no default — so every scenario
 *   states its choice explicitly: '#app' for a scenario that is genuinely a full page/full app, or
 *   a scenario-specific selector (conventionally '#shot') for a single component, so the crop is
 *   that component's own bounding box rather than an arbitrarily large page around it. See
 *   https://github.com/agentydragon/ducktape/pull/3343 for the bug this guards against.
 *   outputName overrides the filename stem for the published PNG (defaults to scenarioName).
 *   colorScheme sets the `prefers-color-scheme` media feature (defaults to 'light').
 *   readySelectors are the scene's own readiness conditions: what must be on the page before it
 *   is the scene at all — a fetch's result, a lazily-mounted component. Mounting is not enough
 *   for those, and neither is `waitForStable`, which knows about fonts and paint but nothing
 *   about a scene's content. A scene with nothing arriving after mount passes none.
 */
export async function main(scenarioName, options) {
  if (!options?.element) {
    throw new Error(
      "main() requires options.element: pass '#app' for a genuine full-page scenario, or a " +
        "scenario-specific selector (e.g. '#shot') for a single component — there is no default, " +
        "so every scenario states which one it is."
    );
  }
  const outputName = options.outputName || scenarioName;

  const harnessPath = process.env.HARNESS_PATH || join(__dirname, "harness/dist/harness.js");
  const distDir = dirname(harnessPath);
  const harnessDir = distDir.endsWith("/dist") ? dirname(distDir) : distDir;
  const outputDir = process.env.TEST_UNDECLARED_OUTPUTS_DIR || join(__dirname, "renders");

  const indexPath = resolve(join(harnessDir, "index.html"));
  if (!existsSync(indexPath)) {
    console.error(`Harness index.html not found in: ${harnessDir}`);
    process.exit(1);
  }

  const userDataDir = join(process.env.TEST_TMPDIR || process.cwd(), `chrome-user-data-${outputName}`);
  mkdirSync(userDataDir, { recursive: true });

  // --single-process + file access: the harness is loaded from a file:// URL.
  const browser = await launchDeterministicBrowser({
    args: ["--single-process", "--allow-file-access-from-files"],
    userDataDir,
  });
  let passed = false;

  try {
    const page = await browser.newPage();
    // Render health: any uncaught error in the page fails the test — with no
    // pixel gate this is the primary crash detector.
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error));
    const viewport = { width: 1200, height: 800, deviceScaleFactor: 1, ...options.viewport };
    const colorScheme = options.colorScheme || "light";
    // Freezing the wall clock keeps time-relative formatters (e.g. date-fns
    // formatDistanceToNow used by formatAge) deterministic — without it, renders drift
    // as the mock dates cross date-fns thresholds ("about 1 year" → "over 1 year", etc.).
    await prepareDeterministicPage(page, { viewport, colorScheme });
    // The harness is entirely local (file:// page, bundled fixtures), so nothing may reach the
    // network; a request that tries is aborted and fails the scenario by name before capture.
    const escapedRequests = await abortUnexpectedRequests(page, (request) => request.url().startsWith("file://"));

    const harnessUrl = `file://${indexPath}`;

    // Verify the harness's hermetic font loaded. Most callers use Inter (the
    // shared test-fonts.css default), but a harness can override via the
    // EXPECTED_FONT_FAMILY env var when it bundles its own typography.
    const expectedFont = process.env.EXPECTED_FONT_FAMILY || "Inter";
    await page.goto(harnessUrl, { waitUntil: "networkidle0", timeout: WAIT_TIMEOUT_MS });
    const fontLoaded = await page.evaluate((family) => document.fonts.check(`16px "${family}"`), expectedFont);
    if (!fontLoaded) {
      console.error(`FATAL: ${expectedFont} font did not load`);
      process.exit(1);
    }

    console.log(`Testing: ${outputName} (page=${scenarioName})`);
    await page.goto(`${harnessUrl}?page=${scenarioName}`, { waitUntil: "networkidle0", timeout: WAIT_TIMEOUT_MS });
    await page.waitForSelector("#app > *", { timeout: WAIT_TIMEOUT_MS });
    for (const selector of options.readySelectors ?? []) {
      await page.waitForSelector(selector, { timeout: WAIT_TIMEOUT_MS });
    }
    // Last, so fonts, images and paint settle around whatever the scene's own conditions let in.
    await waitForStable(page);
    // No-op today (this harness stubs no fetch, so no page installs the ledger); wired so a
    // future harness that does stub one is gated without touching this driver.
    await assertNetworkSettled(page, { context: outputName });
    if (escapedRequests.length > 0) {
      throw new Error(`${outputName}: requests escaped the harness:\n  ${escapedRequests.join("\n  ")}`);
    }

    const screenshot = await screenshotElement(page, options.element, { context: "main()" });

    if (!existsSync(outputDir)) mkdirSync(outputDir, { recursive: true });
    writeFileSync(join(outputDir, `${outputName}-actual.png`), screenshot);

    // Publish the render for PR visual review (devinfra/pr_visuals/publisher.py
    // picks the manifest up from passing CI runs). Upsert so a runner invoking
    // main() for several scenarios accumulates one manifest.
    upsertVisualReviewAsset(outputDir, {
      title: outputName,
      asset: { path: `${outputName}-actual.png`, label: outputName },
    });

    if (pageErrors.length > 0) {
      console.error(`  ✗ ${pageErrors.length} browser page error(s):`);
      for (const error of pageErrors) console.error(`    ${error.stack || error}`);
    } else {
      console.log("  ✓ Passed");
      passed = true;
    }
  } finally {
    await browser.close();
  }

  process.exit(passed ? 0 : 1);
}
