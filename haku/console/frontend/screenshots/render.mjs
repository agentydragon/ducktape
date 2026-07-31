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
  prepareDeterministicPage,
  screenshotElement,
  settle,
} from "../../../../util/testing/frontend_visual/capture.mjs";
import {
  DISABLE_ANIMATIONS_CSS,
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
    name: "settings-oauth-success",
    viewport: { width: 1200, height: 1000 },
    closeApprovals: true,
    frame: true,
  },
  {
    name: "settings-oauth-error",
    viewport: { width: 1200, height: 1000 },
    closeApprovals: true,
    frame: true,
  },
  {
    name: "agent-enrollment",
    viewport: { width: 1200, height: 900 },
    closeApprovals: true,
    frame: true,
  },
  {
    name: "agent-enrollment-reconnect",
    viewport: { width: 1200, height: 900 },
    closeApprovals: true,
    frame: true,
  },
  {
    name: "agent-enrollment-mobile",
    viewport: { width: 390, height: 760 },
    closeApprovals: true,
    frame: true,
  },
  {
    // The only scene that renders the detailed variant: its first row is toggled to Full and that
    // row's Metadata disclosure opened, so detail-only rendering is actually visually reviewed.
    name: "history",
    viewport: { width: 1200, height: 1500 },
    closeApprovals: true,
    clicks: ['[aria-label="Full"]', "summary::-p-text(Metadata)"],
    expectVisible: ".haku-shell-disclosure[open] .haku-shell-disclosure-body",
    frame: true,
  },
  {
    // Toggling "Show auto-approved" reveals the unconditionally-auto-approved sample row that
    // the default "history" scene above hides.
    name: "history-auto-approved",
    viewport: { width: 1200, height: 1500 },
    closeApprovals: true,
    clicks: ['[aria-label="Show auto-approved"]'],
    frame: true,
  },
  { name: "sync-current", viewport: { width: 600, height: 420 }, clicks: ['[aria-label="Up to date"]'] },
  { name: "sync-syncing", viewport: { width: 600, height: 420 }, clicks: ['[aria-label="Syncing"]'] },
  { name: "sync-error", viewport: { width: 600, height: 420 }, clicks: ['[aria-label="Sync error"]'] },
  {
    name: "session-expiring",
    viewport: { width: 600, height: 420 },
    clicks: ['[aria-label="Session expiring soon"]'],
  },
  { name: "oauth-success", viewport: { width: 900, height: 700 } },
  { name: "oauth-error", viewport: { width: 900, height: 700 } },
  { name: "oauth-success-mobile", viewport: { width: 390, height: 760 } },
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

// Chromium comes from the BUILD-wired CHROMIUM_HEADLESS_SHELL (hermetic
// @playwright_browsers build under RBE) or the ambient Playwright browser for
// a local `bazel run` — both resolved inside launchDeterministicBrowser. The deterministic flag
// bundle (font hinting/subpixel positioning, LCD text, Skia runtime opts, swiftshader, …) pins
// general rasterization, matching the same launcher every other visual-test consumer uses. This
// + DISABLE_ANIMATIONS_CSS below closes off rendering-level jitter, not async-load races — see
// ../../../../util/testing/frontend_visual/README.md for how to verify a scene is deterministic.
const browser = await launchDeterministicBrowser();
const assets = [];
const mockHakuUi = readInput("MOCK_HAKU_UI", "mock_haku_ui.html");
try {
  for (const colorScheme of COLOR_SCHEMES) {
    for (const {
      name,
      viewport,
      click,
      clicks,
      closeApprovals,
      expectVisible,
      element,
      fullPage = false,
      frame,
    } of SCENES) {
      const page = await browser.newPage();
      // Matches visual-test-lib.mjs's fixed epoch: harness.tsx's chromeProps feeds the real
      // Date.now() into sampleRecentToolCalls, so without a frozen clock any date-relative text
      // (e.g. formatAge) would drift between runs instead of rendering the same value every time.
      await prepareDeterministicPage(page, { viewport: { ...viewport, deviceScaleFactor: 2 }, colorScheme });
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
      await page.goto("https://haku-console.test/", { waitUntil: "load" });
      await page.waitForSelector("#app > *", { timeout: 10_000 });
      if (frame) {
        const hakuFrame = page.frames().find((candidate) => candidate.url().startsWith("https://haku-ui.test/"));
        if (!hakuFrame) throw new Error(`scene ${name}: mocked Haku UI iframe did not load`);
        await hakuFrame.waitForSelector("main", { timeout: 10_000 });
      }
      // Let Mantine mount and layout settle, and outlast the approval buttons' 400ms arm
      // delay so they render enabled (animations are reduced above).
      await settle(700);
      // Close the drawer BEFORE the clicks below, not after. The drawer renders its own tool-call
      // cards, so while it is open its controls shadow the page's — `page.click` takes the first
      // match in DOM order, and a scene meant to toggle a history row would silently toggle the
      // drawer's card instead, then throw that state away when the drawer closed. No scene clicks
      // anything inside the drawer, so establishing the closed baseline first is unambiguous.
      if (closeApprovals) {
        const drawerClose = await page.$('.haku-shell-drawer [aria-label="Close approvals"]');
        if (drawerClose) await drawerClose.click();
        await page.waitForSelector(".haku-shell-drawer", { hidden: true, timeout: 5_000 });
      }
      // Some scenes need clicks to reveal state internal to a component: a popover's open state
      // (location-sharing control) or history rows toggled into their detailed view.
      // Each click re-renders the DOM, so re-settle before the next one.
      for (const selector of clicks ?? (click ? [click] : [])) {
        await page.click(selector);
        await settle(300);
      }
      // A click that lands on the wrong element fails silently — `page.click` only throws when
      // nothing matches at all. `expectVisible` is the scene's own proof that its clicks did what
      // they were written to do.
      if (expectVisible) {
        await page.waitForSelector(expectVisible, { visible: true, timeout: 5_000 });
      }
      if (frame) {
        // haku_ui_embed.tsx's refreshToolApprovals() fires on mount and increments syncsInFlight
        // around a mocked fetch — same class of race as the MCP-server probes below, on the rail's
        // sync-status icon. Only the real-shell (frame) scenes hit this; the isolated sync-* scenes
        // force a specific state via clicks (sync-syncing deliberately wants "Syncing" left showing,
        // so this must not run there).
        await page.waitForSelector('[aria-label="Syncing"]', { hidden: true, timeout: 5_000 });
      }
      // The settings scene's MCP server list resolves through two chained async mock-fetch
      // rounds (list, then a per-connection status probe) — occasionally still in flight past
      // the fixed 700ms settle under RBE scheduling jitter, capturing a spinner instead of the
      // resolved status. Waiting for these loaders to clear is a no-op on scenes that never had
      // them: `hidden: true` is already satisfied for a selector that was never in the DOM.
      await page.waitForSelector('[aria-label="Loading MCP servers"]', { hidden: true, timeout: 5_000 });
      await page.waitForSelector('[aria-label="Loading Agents"]', { hidden: true, timeout: 5_000 });
      await page.waitForSelector('[aria-label="Loading Agent enrollment"]', { hidden: true, timeout: 5_000 });
      await page.waitForSelector('[aria-label="Checking connection status"]', { hidden: true, timeout: 5_000 });
      const file = `${name}-${colorScheme}.png`;
      const shot = element
        ? await screenshotElement(page, element, { context: `scene ${name}` })
        : await page.screenshot({ fullPage });
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
