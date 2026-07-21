// Screenshot generator for the console's full-page visual surfaces (the history view, shell
// chrome, and settings panel). Inlines the compiled stylesheet and bundled harness into a
// headless-Chromium page, one scene and theme per load, and writes a PNG for every combination.
// A generator for eyeballing the visuals — NOT a pixel-diff gate — so it never fails on "looks
// different"; it fails only if a scene crashes or renders an empty #app (waitForSelector throws).
//
// Per-tool preview cards live in their own per-server `:previews` targets under
// tool_rendering/<server>/ (shared driver: tool_rendering/screenshot/render.mjs).
//
// Runs as an RBE js_test (browser rendering needs the RBE worker's display stack — a local Bazel
// can't fetch repos in web sessions), writing the PNGs to the test's undeclared outputs. See
// frontend/AGENTS.md for how to run it and fetch the PNGs.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  DISABLE_ANIMATIONS_CSS,
  frozenClockScript,
  launchDeterministicBrowser,
} from "../../../../util/testing/frontend_visual/launcher.mjs";
import { writeVisualReviewManifest } from "../../../../util/testing/frontend_visual/visual-review-manifest.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));

// Every scene renders the full production shell. Haku UI scenes use an actual iframe whose
// request is intercepted below and answered with a conspicuous striped test document.
const SCENES = [
  {
    name: "console",
    viewport: { width: 1200, height: 800 },
    closeApprovals: true,
    frame: true,
  },
  { name: "console-drawer", viewport: { width: 1200, height: 800 }, frame: true },
  { name: "console-mobile", viewport: { width: 390, height: 760 }, frame: true },
  {
    name: "settings",
    viewport: { width: 1200, height: 1000 },
    closeApprovals: true,
    frame: true,
  },
  {
    name: "history",
    viewport: { width: 1200, height: 1500 },
    closeApprovals: true,
    clicks: ['[aria-label="Full"]', "summary::-p-text(Metadata)"],
    frame: true,
  },
  { name: "sync-current", viewport: { width: 600, height: 420 }, clicks: ['[aria-label="Up to date"]'] },
  { name: "sync-syncing", viewport: { width: 600, height: 420 }, clicks: ['[aria-label="Syncing"]'] },
  { name: "sync-error", viewport: { width: 600, height: 420 }, clicks: ['[aria-label="Sync error"]'] },
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
    `<style>${css}${DISABLE_ANIMATIONS_CSS}</style></head><body><div id='app'></div>`,
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

// Matches visual-test-lib.mjs's fixed epoch: harness.tsx's chromeProps feeds the real
// Date.now() into sampleRecentToolCalls, so without a frozen clock any date-relative text
// (e.g. formatAge) would drift between runs instead of rendering the same value every time.
const FROZEN_NOW_MS = Date.parse("2025-02-01T12:00:00Z");

// Chromium comes from the BUILD-wired CHROMIUM_HEADLESS_SHELL (hermetic
// @playwright_browsers build under RBE) or the ambient Playwright browser for
// a local `bazel run` — both resolved inside launchDeterministicBrowser. The deterministic flag
// bundle (font hinting/subpixel positioning, LCD text, Skia runtime opts, swiftshader, …) pins
// general rasterization, matching the same launcher every other visual-test consumer uses — see
// haku/console/debug/pr_visuals_flaky_diffs.md for the specific bug this + DISABLE_ANIMATIONS_CSS
// below fix (an unguarded Mantine `Indicator processing` CSS animation was the actual cause).
const browser = await launchDeterministicBrowser();
const assets = [];
const mockHakuUi = readInput("MOCK_HAKU_UI", "mock_haku_ui.html");
try {
  for (const colorScheme of COLOR_SCHEMES) {
    for (const { name, viewport, click, clicks, closeApprovals, element, fullPage = false, frame } of SCENES) {
      const page = await browser.newPage();
      await page.evaluateOnNewDocument(frozenClockScript(FROZEN_NOW_MS));
      page.on("console", (message) => console.log(`[${name}] browser: ${message.text()}`));
      page.on("pageerror", (error) => console.error(`[${name}] browser error:`, error));
      await page.setRequestInterception(true);
      const html = pageHtml(css, harnessJs, name, colorScheme);
      page.on("request", (request) => {
        if (request.isNavigationRequest() && request.frame() === page.mainFrame()) {
          void request.respond({ status: 200, contentType: "text/html", body: html });
        } else if (request.url().startsWith("https://haku-ui.test/")) {
          void request.respond({ status: 200, contentType: "text/html", body: mockHakuUi });
        } else {
          void request.continue();
        }
      });
      await page.setViewport({ ...viewport, deviceScaleFactor: 2 });
      await page.emulateMediaFeatures([
        { name: "prefers-reduced-motion", value: "reduce" },
        { name: "prefers-color-scheme", value: colorScheme },
      ]);
      await page.goto("https://haku-console.test/", { waitUntil: "load" });
      await page.waitForSelector("#app > *", { timeout: 10_000 });
      if (frame) {
        const hakuFrame = page.frames().find((candidate) => candidate.url().startsWith("https://haku-ui.test/"));
        if (!hakuFrame) throw new Error(`scene ${name}: mocked Haku UI iframe did not load`);
        await hakuFrame.waitForSelector("main", { timeout: 10_000 });
      }
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
      if (closeApprovals) {
        const drawerClose = await page.$('.haku-shell-drawer [aria-label="Close approvals"]');
        if (drawerClose) await drawerClose.click();
        await page.waitForSelector(".haku-shell-drawer", { hidden: true, timeout: 5_000 });
      }
      // The settings scene's MCP server list resolves through two chained async mock-fetch
      // rounds (list, then a per-connection status probe) — occasionally still in flight past
      // the fixed 700ms settle under RBE scheduling jitter, capturing a spinner instead of the
      // resolved status. Waiting for these loaders to clear is a no-op on scenes that never had
      // them: `hidden: true` is already satisfied for a selector that was never in the DOM.
      await page.waitForSelector('[aria-label="Loading MCP servers"]', { hidden: true, timeout: 5_000 });
      await page.waitForSelector('[aria-label="Checking connection status"]', { hidden: true, timeout: 5_000 });
      const file = `${name}-${colorScheme}.png`;
      let shot;
      if (element) {
        const handle = await page.$(element);
        if (!handle) throw new Error(`scene ${name}: element ${element} not found`);
        shot = await handle.screenshot();
      } else {
        shot = await page.screenshot({ fullPage });
      }
      writeFileSync(join(outDir, file), shot);
      assets.push({ path: file, label: `${name} - ${colorScheme}` });
      console.log(`wrote ${join(outDir, file)}`);
      await page.close();
    }
  }
} finally {
  await browser.close();
}
writeVisualReviewManifest(outDir, {
  title: "Haku Console",
  assets,
});
console.log(`\n${assets.length} screenshots in ${outDir}`);
