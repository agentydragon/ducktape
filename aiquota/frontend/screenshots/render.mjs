/**
 * Renders the dashboard once per shared scenario, in both themes, and publishes the PNGs for
 * PR visual review — the browser counterpart of the GNOME extension's `test_render.py`, over
 * the same fixtures (`aiquota/testing/fixtures/`).
 *
 * It is a render-health gate, not a pixel gate: it fails when a scene throws, mounts nothing,
 * or lets a request escape the harness — never on "looks different". What changed is reviewed
 * on the PR's visual-review page (devinfra/pr_visuals/publisher.py).
 *
 * Browser rendering needs the RBE worker's display stack, so this runs as a js_test rather
 * than a `bazel run` binary.
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  abortUnexpectedRequests,
  prepareDeterministicPage,
  screenshotElement,
  waitForStable,
  WAIT_TIMEOUT_MS,
} from "../../../util/testing/frontend_visual/capture.mjs";
import { launchDeterministicBrowser } from "../../../util/testing/frontend_visual/launcher.mjs";
import { writeVisualReviewManifest } from "../../../util/testing/frontend_visual/visual-review-manifest.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));

const DESKTOP = { width: 1200, height: 900 };
// One narrow shot: the provider grid collapses to a column and the heading stacks, and that
// reflow is the part of the layout a desktop-only sweep would never exercise.
const NARROW = { width: 420, height: 900 };

function scenePath() {
  const fromEnv = process.env.SCENES_JSON;
  return fromEnv && existsSync(fromEnv) ? fromEnv : join(HERE, "..", "fixtures", "scenes.json");
}

function outputDir() {
  return process.env.TEST_UNDECLARED_OUTPUTS_DIR ?? resolve("screenshot-out");
}

const scenes = Object.keys(JSON.parse(readFileSync(scenePath(), "utf8")));
const shots = [
  ...["light", "dark"].flatMap((theme) =>
    scenes.map((scene) => ({ name: `${scene}-${theme}`, scene, theme, viewport: DESKTOP }))
  ),
  { name: "hot-narrow", scene: "hot", theme: "dark", viewport: NARROW },
];

const indexUrl = `file://${resolve(join(HERE, "index.html"))}`;
const outDir = outputDir();
mkdirSync(outDir, { recursive: true });

const browser = await launchDeterministicBrowser({ args: ["--allow-file-access-from-files"] });
const assets = [];
const failures = [];
try {
  for (const { name, scene, theme, viewport } of shots) {
    const page = await browser.newPage();
    // One broken scene must not hide the rest: record it and carry on, so a single run
    // enumerates everything that is wrong.
    try {
      const pageErrors = [];
      page.on("pageerror", (error) => pageErrors.push(error));
      await prepareDeterministicPage(page, { viewport: { ...viewport, deviceScaleFactor: 2 }, colorScheme: theme });
      // The harness is entirely local; anything reaching for the network is a hole in it.
      const escaped = await abortUnexpectedRequests(page, (request) => request.url().startsWith("file://"));

      await page.goto(`${indexUrl}?scene=${encodeURIComponent(scene)}`, {
        waitUntil: "networkidle0",
        timeout: WAIT_TIMEOUT_MS,
      });
      await page.waitForSelector(".aiquota-card", { timeout: WAIT_TIMEOUT_MS });
      await waitForStable(page);
      if (escaped.length > 0) throw new Error(`requests escaped the harness:\n  ${escaped.join("\n  ")}`);
      if (pageErrors.length > 0) throw new Error(pageErrors.map((error) => error.stack ?? String(error)).join("\n"));

      const file = `${name}.png`;
      writeFileSync(join(outDir, file), await screenshotElement(page, "#app", { context: name }));
      assets.push({ path: file, label: `${scene} · ${theme}${viewport === NARROW ? " · narrow" : ""}` });
      console.log(`wrote ${join(outDir, file)}`);
    } catch (error) {
      failures.push(`${name}: ${error.message}`);
    } finally {
      await page.close();
    }
  }
} finally {
  await browser.close();
}

if (failures.length > 0) {
  console.error(`${failures.length} scene(s) failed:\n  ${failures.join("\n  ")}`);
  process.exit(1);
}

writeVisualReviewManifest(outDir, { title: "aiquota dashboard", assets });
