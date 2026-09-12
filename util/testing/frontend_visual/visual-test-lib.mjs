/**
 * Shared infrastructure for visual render-health tests.
 *
 * Two entry points over one implementation. `main(name, options)` runs a single scenario, for a
 * package that gives every scenario its own js_test target. `runScenarios(table, {title})` sweeps
 * a whole table on one browser, for a package that owns every scenario in one target and splits
 * them with `shard_count` -- there the scenario list lives only in the table, never repeated in
 * BUILD, and `--test_filter=<scenario>` addresses one of them.
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
 * The scenarios this process is responsible for: `--test_filter` narrows the table, and Bazel's
 * shard environment splits what remains. Filtering first keeps a one-scenario filter addressable
 * whichever shard would otherwise own it — the other shards then run nothing and pass.
 */
function selectScenarios(names) {
  // Bazel honours shard_count only for a runner that advertises support by touching this file,
  // and it must be touched whether or not this shard ends up owning any scenario.
  const statusFile = process.env.TEST_SHARD_STATUS_FILE;
  if (statusFile) writeFileSync(statusFile, "");
  const filter = process.env.TESTBRIDGE_TEST_ONLY;
  const matched = filter ? names.filter((name) => name.includes(filter)) : names;
  if (filter && matched.length === 0) {
    throw new Error(`--test_filter=${filter} matched none of: ${names.join(", ")}`);
  }
  const total = Number(process.env.TEST_TOTAL_SHARDS ?? 1);
  const index = Number(process.env.TEST_SHARD_INDEX ?? 0);
  return matched.filter((_, position) => position % total === index);
}

async function captureScenario(browser, scenarioName, options, { harnessUrl, outputDir, title }) {
  if (!options?.element) {
    throw new Error(
      "every scenario needs options.element: '#app' for a genuine full-page scenario, or a " +
        "scenario-specific selector (e.g. '#shot') for a single component — there is no default, " +
        "so every scenario states which one it is."
    );
  }
  const outputName = options.outputName || scenarioName;
  const page = await browser.newPage();
  try {
    // Render health: any uncaught error in the page fails the scenario — with no
    // pixel gate this is the primary crash detector.
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error));
    // Freezing the wall clock keeps time-relative formatters (e.g. date-fns
    // formatDistanceToNow used by formatAge) deterministic — without it, renders drift
    // as the mock dates cross date-fns thresholds ("about 1 year" → "over 1 year", etc.).
    await prepareDeterministicPage(page, {
      viewport: { width: 1200, height: 800, deviceScaleFactor: 1, ...options.viewport },
      colorScheme: options.colorScheme || "light",
    });
    // The harness is entirely local (file:// page, bundled fixtures), so nothing may reach the
    // network; a request that tries is aborted and fails the scenario by name before capture.
    const escapedRequests = await abortUnexpectedRequests(page, (request) => request.url().startsWith("file://"));

    console.log(`Testing: ${outputName} (page=${scenarioName})`);
    await page.goto(`${harnessUrl}?page=${scenarioName}`, { waitUntil: "networkidle0", timeout: WAIT_TIMEOUT_MS });
    // test-fonts.css applies the hermetic font to every element, so any harness document carries
    // it; a miss means the @font-face never resolved and every glyph below is the wrong shape.
    // Most callers use Inter (the shared test-fonts.css default), but a harness can override via
    // the EXPECTED_FONT_FAMILY env var when it bundles its own typography.
    const expectedFont = process.env.EXPECTED_FONT_FAMILY || "Inter";
    if (!(await page.evaluate((family) => document.fonts.check(`16px "${family}"`), expectedFont))) {
      throw new Error(`${expectedFont} font did not load`);
    }
    await page.waitForSelector("#app > *", { timeout: WAIT_TIMEOUT_MS });
    for (const selector of options.readySelectors ?? []) {
      await page.waitForSelector(selector, { timeout: WAIT_TIMEOUT_MS });
    }
    // Last, so fonts, images and paint settle around whatever the scene's own conditions let in.
    await waitForStable(page);
    await assertNetworkSettled(page, { context: outputName });
    if (escapedRequests.length > 0) {
      throw new Error(`requests escaped the harness:\n    ${escapedRequests.join("\n    ")}`);
    }

    // Viewport captures preserve clipping instead of expanding to fit an overflowing app.
    const screenshot = options.captureViewport
      ? await page.screenshot({ fullPage: false })
      : await screenshotElement(page, options.element, { context: outputName });
    writeFileSync(join(outputDir, `${outputName}-actual.png`), screenshot);

    // Publish the render for PR visual review (devinfra/pr_visuals/publisher.py
    // picks the manifest up from passing CI runs). Upsert so a sweep accumulates
    // one manifest across every scenario it rendered.
    upsertVisualReviewAsset(outputDir, {
      title,
      asset: { path: `${outputName}-actual.png`, label: outputName },
    });

    if (pageErrors.length > 0) {
      const detail = pageErrors.map((error) => error.stack || error).join("\n    ");
      throw new Error(`${pageErrors.length} browser page error(s):\n    ${detail}`);
    }
    console.log("  ✓ Passed");
  } finally {
    await page.close();
  }
}

/**
 * Render every scenario this shard owns on one browser and exit 0 (pass) or 1 (fail).
 *
 * @param {Record<string, object>} scenarios - Harness page name to its capture options:
 *   element is the CSS selector to screenshot. Required — there is no default — so every scenario
 *   states its choice explicitly: '#app' for a scenario that is genuinely a full page/full app, or
 *   a scenario-specific selector (conventionally '#shot') for a single component, so the crop is
 *   that component's own bounding box rather than an arbitrarily large page around it. See
 *   https://github.com/agentydragon/ducktape/pull/3343 for the bug this guards against.
 *   outputName overrides the filename stem for the published PNG (defaults to the page name).
 *   colorScheme sets the `prefers-color-scheme` media feature (defaults to 'light').
 *   readySelectors are the scene's own readiness conditions: what must be on the page before it
 *   is the scene at all — a fetch's result, a lazily-mounted component. Mounting is not enough
 *   for those, and neither is `waitForStable`, which knows about fonts and paint but nothing
 *   about a scene's content. A scene with nothing arriving after mount passes none.
 *   captureViewport screenshots the viewport rather than the element, preserving clipping.
 * @param {{ title: string }} options - Title for the published visual-review manifest.
 */
export async function runScenarios(scenarios, { title }) {
  const harnessPath = process.env.HARNESS_PATH || join(__dirname, "harness/dist/harness.js");
  const distDir = dirname(harnessPath);
  const harnessDir = distDir.endsWith("/dist") ? dirname(distDir) : distDir;
  const indexPath = resolve(join(harnessDir, "index.html"));
  if (!existsSync(indexPath)) throw new Error(`Harness index.html not found in: ${harnessDir}`);
  const outputDir = process.env.TEST_UNDECLARED_OUTPUTS_DIR || join(__dirname, "renders");
  mkdirSync(outputDir, { recursive: true });

  const selected = selectScenarios(Object.keys(scenarios));
  // A shard with nothing to render must not start Chromium: `--single-process` with no page ever
  // opened wedges in browser.close(), which under --test_filter (where every shard but one is
  // empty) means a 900s timeout per shard instead of an instant pass.
  if (selected.length === 0) {
    console.log(`shard ${process.env.TEST_SHARD_INDEX ?? 0}: no scenario to render`);
    process.exit(0);
  }
  const userDataDir = join(
    process.env.TEST_TMPDIR || process.cwd(),
    `chrome-user-data-${title.replace(/[^A-Za-z0-9]+/g, "-")}`
  );
  mkdirSync(userDataDir, { recursive: true });

  // --single-process + file access: the harness is loaded from a file:// URL.
  const browser = await launchDeterministicBrowser({
    args: ["--single-process", "--allow-file-access-from-files"],
    userDataDir,
  });
  const failures = [];
  try {
    for (const scenarioName of selected) {
      // One scenario must not hide the rest: a failure is recorded and the sweep continues, so a
      // single run enumerates every broken scene rather than stopping at the first.
      try {
        await captureScenario(browser, scenarioName, scenarios[scenarioName], {
          harnessUrl: `file://${indexPath}`,
          outputDir,
          title,
        });
      } catch (error) {
        console.error(`  ✗ ${scenarioName}: ${error.message}`);
        failures.push(`${scenarioName}: ${error.message}`);
      }
    }
  } finally {
    await browser.close();
  }
  if (failures.length > 0) {
    console.error(`\n${failures.length} scenario(s) failed:\n  ${failures.join("\n  ")}`);
    process.exit(1);
  }
  process.exit(0);
}

/**
 * Run a single visual scenario and exit 0 (pass) or 1 (fail).
 * Called from each per-scenario test file.
 *
 * @param {string} scenarioName - Harness page name (e.g. "ListPage").
 * @param {object} options - This scenario's capture options; see runScenarios.
 */
export async function main(scenarioName, options) {
  await runScenarios({ [scenarioName]: options }, { title: options?.outputName || scenarioName });
}
