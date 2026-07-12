// Screenshot generator for the console's own visual surfaces. Inlines the compiled
// stylesheet and the bundled harness (harness.tsx) into a headless-Chromium page, one
// scene and theme per load, and writes a PNG for every combination. This is a generator for eyeballing the
// visuals — NOT a pixel-diff regression gate — so it never fails on "looks different"; it
// fails only if a scene crashes or renders an empty #app (waitForSelector below throws).
//
// It runs as an RBE js_test (browser rendering needs the RBE worker's display stack — a
// local Bazel can't fetch repos in web sessions), writing the PNGs to the test's
// undeclared outputs. See frontend/AGENTS.md for how to run it and fetch the PNGs.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { launchPuppeteerBrowser } from "../../../../util/testing/frontend_visual/puppeteer-lib.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));

// The console's full-page surfaces (ToolCallsPage, ShellChrome) are position:fixed, so a
// viewport screenshot — not an #app element shot — is what captures them. Each scene is a
// separate page load driven by window.__SCENE__ (see harness.tsx).
const SCENES = [
  // The history page, showing both row states in one shot: flip the first row's Brief/Full
  // selector to Full (its segments are icons, so match the "Full" icon by aria-label) and open
  // its Metadata disclosure, leaving the rest Brief. `[aria-label="Full"]` matches the first
  // row's Full segment; the Metadata summary only exists once that row is detailed.
  {
    name: "history",
    viewport: { width: 1200, height: 1500 },
    clicks: ['[aria-label="Full"]', "summary::-p-text(Metadata)"],
  },
  { name: "settings", viewport: { width: 1200, height: 900 } },
  // Every implemented tool-call preview, compact | detailed side by side — tall, so give it room.
  { name: "previews", viewport: { width: 1100, height: 4800 } },
  // The whole shell chrome: approvals panel open by default; the clicks open the live-offline
  // and location panels (their open state is internal to the chrome) so the shot shows all
  // three surfaces stacked by Y under the toggle-button row.
  {
    name: "chrome",
    viewport: { width: 860, height: 1040 },
    clicks: ['[aria-label="Live updates disconnected"]', '[aria-label="Location sharing: live"]'],
  },
];
const COLOR_SCHEMES = ["light", "dark"];

function outputDir() {
  if (process.env.SCREENSHOT_OUT_DIR) return resolve(process.env.SCREENSHOT_OUT_DIR);
  // Under `bbr test` the PNGs go to the test's undeclared outputs (fetched via the
  // buildbuddy_api skill). Under a local `bazel run` (BUILD_WORKSPACE_DIRECTORY set) they
  // land in the source tree for trivial opening.
  if (process.env.TEST_UNDECLARED_OUTPUTS_DIR) return process.env.TEST_UNDECLARED_OUTPUTS_DIR;
  if (process.env.BUILD_WORKSPACE_DIRECTORY) {
    return join(process.env.BUILD_WORKSPACE_DIRECTORY, "haku/console/frontend/screenshots/out");
  }
  return resolve("screenshot-out");
}

function pageHtml(css, harnessJs, scene, colorScheme) {
  // The <base href> gives the origin-less setContent page a URL against which the SPA's
  // relative "/api/…" requests parse (the harness stubs the actual fetch). The scene global
  // is set before the harness IIFE runs so it renders the right surface.
  return [
    "<!doctype html><html><head><meta charset='utf-8'>",
    "<base href='https://haku-console.test/'>",
    `<style>${css}</style></head><body><div id='app'></div>`,
    `<script>window.__SCENE__=${JSON.stringify(scene)};</script>`,
    `<script>window.__COLOR_SCHEME__=${JSON.stringify(colorScheme)};</script>`,
    `<script>${harnessJs}</script></body></html>`,
  ].join("");
}

// Prefer the BUILD-wired $(rootpath) env (resolves against the js_test runfiles-root CWD);
// fall back to this script's runfiles-relative location, where the harness bundle and
// generated stylesheet sit at fixed workspace-relative paths alongside it.
function readInput(envName, ...fallback) {
  const fromEnv = process.env[envName];
  const path = fromEnv && existsSync(fromEnv) ? fromEnv : join(HERE, ...fallback);
  return readFileSync(path, "utf8");
}

const css = readInput("STYLES_CSS", "..", "generated", "styles.css");
const harnessJs = readInput("HARNESS_JS", "dist", "harness.js");
const outDir = outputDir();
mkdirSync(outDir, { recursive: true });

// Chromium: the BUILD wires PUPPETEER_EXECUTABLE_PATH to the hermetic @playwright_browsers
// build under RBE; fall back to the ambient Playwright browser for a local `bazel run`.
if (!process.env.PUPPETEER_EXECUTABLE_PATH && process.env.PLAYWRIGHT_BROWSERS_PATH) {
  process.env.PUPPETEER_EXECUTABLE_PATH = join(process.env.PLAYWRIGHT_BROWSERS_PATH, "chromium");
}

const browser = await launchPuppeteerBrowser({
  args: [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--force-color-profile=srgb",
  ],
});
try {
  for (const colorScheme of COLOR_SCHEMES) {
    for (const { name, viewport, click, clicks } of SCENES) {
      const page = await browser.newPage();
      await page.setViewport({ ...viewport, deviceScaleFactor: 2 });
      await page.emulateMediaFeatures([
        { name: "prefers-reduced-motion", value: "reduce" },
        { name: "prefers-color-scheme", value: colorScheme },
      ]);
      await page.setContent(pageHtml(css, harnessJs, name, colorScheme), { waitUntil: "load" });
      await page.waitForSelector("#app > *", { timeout: 10_000 });
      // Let Mantine mount and layout settle, and outlast the approval buttons' 400ms arm
      // delay so they render enabled (animations are reduced above).
      await new Promise((r) => setTimeout(r, 700));
      // Some scenes need clicks to reveal state internal to a component: a popover's open state
      // (location-sharing control) or history rows toggled into their detailed view.
      // Each click re-renders the DOM, so re-settle before the next one.
      for (const selector of clicks ?? (click ? [click] : [])) {
        await page.click(selector);
        await new Promise((r) => setTimeout(r, 300));
      }
      const dest = join(outDir, `${name}-${colorScheme}.png`);
      const png = await page.screenshot();
      writeFileSync(dest, png);
      console.log(`wrote ${dest}`);
      await page.close();
    }
  }
} finally {
  await browser.close();
}
console.log(`\n${SCENES.length * COLOR_SCHEMES.length} screenshots in ${outDir}`);
