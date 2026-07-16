// Shared driver for per-server preview screenshot targets. Bundles aside, each `preview_screenshots`
// target runs this: it loads the harness (one ToolCallCard) once per fixture × variant × color
// scheme, element-screenshots `.haku-preview-card`, and writes a visual-review.json for that
// server. A generator for eyeballing the visuals, NOT a pixel-diff gate — it passes as long as
// every fixture renders.
//
// Runs as an RBE js_test (the worker's display stack is needed for headless Chromium). See
// haku/console/frontend/AGENTS.md.
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { launchBrowser } from "../../../../../util/testing/frontend_visual/launcher.mjs";
import { writeVisualReviewManifest } from "../../../../../util/testing/frontend_visual/visual-review-manifest.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const COLOR_SCHEMES = ["light", "dark"];
const PREVIEW_VARIANTS = ["compact", "detailed"];
// Matches the approvals panel's width: `width: min(32rem, …)` in frontend/styles.src.css.
const PREVIEW_WIDTH = 512;

function outputDir() {
  if (process.env.SCREENSHOT_OUT_DIR) return resolve(process.env.SCREENSHOT_OUT_DIR);
  // Under `bbr test` the PNGs go to the test's undeclared outputs (fetched via the
  // buildbuddy_api skill). Under a local `bazel run` they land in the source tree.
  if (process.env.TEST_UNDECLARED_OUTPUTS_DIR) return process.env.TEST_UNDECLARED_OUTPUTS_DIR;
  if (process.env.BUILD_WORKSPACE_DIRECTORY) {
    return join(process.env.BUILD_WORKSPACE_DIRECTORY, "haku/console/frontend/tool_rendering/screenshot/out");
  }
  return resolve("screenshot-out");
}

// HARNESS_JS is the native esbuild bundle: the TreeArtifact dir (entryNames ⇒ harness.js inside,
// but we glob so the exact name doesn't matter) or the file itself. STYLES_CSS resolves directly.
function readInput(envName, ...fallback) {
  const fromEnv = process.env[envName];
  if (fromEnv && existsSync(fromEnv)) {
    if (envName === "HARNESS_JS" && statSync(fromEnv).isDirectory()) {
      const js = readdirSync(fromEnv).find((f) => f.endsWith(".js"));
      if (!js) throw new Error(`no harness .js inside bundle dir ${fromEnv}`);
      return readFileSync(join(fromEnv, js), "utf8");
    }
    return readFileSync(fromEnv, "utf8");
  }
  return readFileSync(join(HERE, ...fallback), "utf8");
}

function pageHtml(css, harnessJs, colorScheme, globals = {}) {
  // The <base href> gives the origin-less setContent page a URL against which the SPA's relative
  // "/api/…" requests parse (the harness stubs the actual fetch). Globals are set before the
  // harness IIFE runs so it renders the requested fixture/variant.
  const injectGlobals = Object.entries(globals)
    .map(([key, value]) => `window.${key}=${JSON.stringify(value)};`)
    .join("");
  return [
    "<!doctype html><html><head><meta charset='utf-8'>",
    "<base href='https://haku-console.test/'>",
    `<style>${css}</style></head><body><div id='app'></div>`,
    `<script>window.__SCENE__="preview";</script>`,
    `<script>window.__COLOR_SCHEME__=${JSON.stringify(colorScheme)};</script>`,
    `<script>${injectGlobals}</script>`,
    `<script>${harnessJs}</script></body></html>`,
  ].join("");
}

const css = readInput("STYLES_CSS", "..", "..", "generated", "styles.css");
const harnessJs = readInput("HARNESS_JS", "dist", "harness.js");
const outDir = outputDir();
mkdirSync(outDir, { recursive: true });

// Chromium comes from the BUILD-wired CHROMIUM_HEADLESS_SHELL (hermetic
// @playwright_browsers build under RBE) or the ambient Playwright browser for
// a local `bazel run` — both resolved inside launchBrowser.
const browser = await launchBrowser({ args: ["--force-color-profile=srgb"] });
const assets = [];
try {
  // Discover the fixture manifest (slug + label per sample) the harness exposes, then capture one
  // page per fixture × variant × scheme. Each page renders a single card in isolation at the real
  // approvals-panel width — no shared gallery, so a card's width is exactly the viewport.
  const discover = await browser.newPage();
  await discover.setContent(pageHtml(css, harnessJs, COLOR_SCHEMES[0], { __FIXTURE__: 0, __VARIANT__: "compact" }), {
    waitUntil: "load",
  });
  const fixtures = await discover.evaluate(() => globalThis.__PREVIEW_FIXTURES__);
  await discover.close();
  if (!Array.isArray(fixtures) || fixtures.length === 0) {
    throw new Error("harness did not expose a non-empty window.__PREVIEW_FIXTURES__");
  }
  // Fixture is the outer loop so each tool's compact/detailed × light/dark cluster together in
  // the manifest (and thus on the review page).
  for (const [index, { slug, label }] of fixtures.entries()) {
    for (const colorScheme of COLOR_SCHEMES) {
      for (const variant of PREVIEW_VARIANTS) {
        const page = await browser.newPage();
        await page.setViewport({ width: PREVIEW_WIDTH, height: 900, deviceScaleFactor: 2 });
        await page.emulateMediaFeatures([
          { name: "prefers-reduced-motion", value: "reduce" },
          { name: "prefers-color-scheme", value: colorScheme },
        ]);
        await page.setContent(pageHtml(css, harnessJs, colorScheme, { __FIXTURE__: index, __VARIANT__: variant }), {
          waitUntil: "load",
        });
        await page.waitForSelector(".haku-preview-card", { timeout: 10_000 });
        // Let Mantine mount and any mock-backed widget (gmail subjects, grocy reference, calendar
        // name) fetch + re-render before the screenshot.
        await new Promise((r) => setTimeout(r, 700));
        const card = await page.$(".haku-preview-card");
        const file = `preview-${slug}-${variant}-${colorScheme}.png`;
        writeFileSync(join(outDir, file), await card.screenshot());
        assets.push({ path: file, label: `${label} — ${variant} · ${colorScheme}` });
        console.log(`wrote ${join(outDir, file)}`);
        await page.close();
      }
    }
  }
} finally {
  await browser.close();
}
writeVisualReviewManifest(outDir, { title: "Haku Console previews", assets });
console.log(`\n${assets.length} screenshots in ${outDir}`);
