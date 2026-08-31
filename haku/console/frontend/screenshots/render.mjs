// Screenshot generator for the console's full-page visual surfaces (the history view, shell
// chrome, and settings panel). Inlines the compiled stylesheet and bundled harness into a
// headless-Chromium page, one scene and theme per load, and writes a PNG for every combination.
// A generator for eyeballing the visuals — NOT a pixel-diff gate — so it never fails on "looks
// different"; it fails if a scene crashes, renders an empty #app (waitForSelector throws), lets a
// request escape the mocks (aborted and named below), or captures before its stubbed network
// settled cleanly (assertNetworkSettled).
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
  assertNetworkSettled,
  prepareDeterministicPage,
  screenshotElement,
  waitForStable,
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
  { name: "aiquota", viewport: { width: 1200, height: 800 } },
  { name: "console-drawer", viewport: { width: 1200, height: 800 }, frame: true },
  { name: "console-mobile", viewport: { width: 390, height: 760 }, frame: true },
  {
    name: "not-found",
    viewport: { width: 900, height: 600 },
    closeApprovals: true,
    expectVisible: "::-p-text(Page not found)",
    frame: true,
  },
  {
    name: "approvals-embed",
    viewport: { width: 560, height: 820 },
    expectVisible: "button::-p-text(Approve)",
  },
  {
    name: "settings",
    viewport: { width: 1200, height: 1000 },
    closeApprovals: true,
    frame: true,
  },
  {
    name: "settings-mobile",
    viewport: { width: 390, height: 760 },
    closeApprovals: true,
    frame: true,
  },
  {
    name: "settings-agents",
    viewport: { width: 1200, height: 1000 },
    closeApprovals: true,
    clicks: ['[role="tab"]::-p-text(Agents)'],
    expectVisible: "::-p-text(Public Coder)",
    frame: true,
  },
  {
    name: "settings-grants",
    viewport: { width: 1200, height: 1000 },
    closeApprovals: true,
    clicks: ['[role="tab"]::-p-text(Grants)'],
    expectVisible: "::-p-text(Public Coder)",
    frame: true,
  },
  {
    name: "settings-grants-history",
    viewport: { width: 1200, height: 1000 },
    closeApprovals: true,
    clicks: ['[role="tab"]::-p-text(Grants)', "::-p-text(History)"],
    expectVisible: "::-p-text(Pilot complete; return to standard diagnostics.)",
    frame: true,
  },
  {
    name: "settings-grants-revoke",
    viewport: { width: 1200, height: 1000 },
    closeApprovals: true,
    clicks: ['[role="tab"]::-p-text(Grants)', "button::-p-text(Revoke)"],
    expectVisible: "::-p-text(Confirm)",
    frame: true,
  },
  {
    name: "settings-sessions",
    viewport: { width: 1200, height: 1000 },
    closeApprovals: true,
    clicks: ['[role="tab"]::-p-text(Sessions)'],
    expectVisible: "::-p-text(Waiting for Pod readiness)",
    frame: true,
  },
  {
    name: "settings-sessions-mobile",
    viewport: { width: 390, height: 760 },
    closeApprovals: true,
    clicks: ['[role="tab"]::-p-text(Sessions)'],
    expectVisible: "::-p-text(Waiting for Pod readiness)",
    frame: true,
  },
  {
    name: "settings-sessions-terminate",
    viewport: { width: 1200, height: 1000 },
    closeApprovals: true,
    clicks: ['[role="tab"]::-p-text(Sessions)', "button::-p-text(Terminate)"],
    expectVisible: "::-p-text(Yes, terminate)",
    frame: true,
  },
  {
    name: "settings-sessions-terminate-mobile",
    viewport: { width: 390, height: 760 },
    closeApprovals: true,
    clicks: ['[role="tab"]::-p-text(Sessions)', "button::-p-text(Terminate)"],
    expectVisible: "::-p-text(Yes, terminate)",
    frame: true,
  },
  {
    name: "settings-notifications",
    viewport: { width: 1200, height: 1000 },
    closeApprovals: true,
    clicks: ['[role="tab"]::-p-text(Notifications)'],
    expectVisible: "::-p-text(This browser)",
    frame: true,
  },
  {
    name: "settings-nodes",
    viewport: { width: 1200, height: 1000 },
    closeApprovals: true,
    clicks: ['[role="tab"]::-p-text(Nodes)'],
    expectVisible: '[aria-label="Node status: busy"]',
    frame: true,
  },
  {
    name: "settings-nodes-mobile",
    viewport: { width: 390, height: 900 },
    closeApprovals: true,
    clickTabText: "Nodes",
    expectVisible: '[aria-label="Node status: busy"]',
    frame: true,
  },
  {
    name: "settings-system",
    viewport: { width: 1200, height: 1000 },
    closeApprovals: true,
    clicks: ['[role="tab"]::-p-text(System)'],
    expectVisible: "::-p-text(Mixed revisions)",
    frame: true,
  },
  // A transcript long enough to overflow, so the scroll opens pinned to the newest message.
  {
    name: "conversation-overflow",
    viewport: { width: 1200, height: 700 },
    closeApprovals: true,
    expectVisible: "::-p-text(Latest answer)",
    frame: true,
  },
  {
    name: "conversation-tool-use",
    viewport: { width: 1200, height: 1000 },
    closeApprovals: true,
    expectVisible: "::-p-text(mcp__haku-console__haku-console__list_mcp_servers)",
    frame: true,
  },
  {
    name: "conversation-tool-use-mobile",
    viewport: { width: 390, height: 1000 },
    closeApprovals: true,
    expectVisible: "::-p-text(mcp__haku-console__haku-console__list_mcp_servers)",
    frame: true,
  },
  // The sandbox still being handed out: the live Kubernetes read, which is the whole account for
  // a session that never comes up.
  {
    name: "conversation-provisioning",
    viewport: { width: 1200, height: 900 },
    closeApprovals: true,
    expectVisible: "::-p-text(Waiting for Pod ready)",
    frame: true,
  },
  {
    name: "conversation-provisioning-mobile",
    viewport: { width: 390, height: 900 },
    closeApprovals: true,
    expectVisible: "::-p-text(Waiting for Pod ready)",
    frame: true,
  },
  {
    name: "conversations",
    viewport: { width: 1200, height: 900 },
    closeApprovals: true,
    expectVisible: "::-p-text(Load older conversations)",
    frame: true,
  },
  {
    name: "conversations-mobile",
    viewport: { width: 390, height: 900 },
    closeApprovals: true,
    expectVisible: "::-p-text(!ops:example.org)",
    frame: true,
  },
  {
    name: "conversation-detail",
    viewport: { width: 1200, height: 1000 },
    closeApprovals: true,
    expectVisible: "::-p-text(Claude Desktop)",
    frame: true,
  },
  {
    name: "conversation-detail-mobile",
    viewport: { width: 390, height: 1000 },
    closeApprovals: true,
    // The newest message, since the transcript opens scrolled to it.
    expectVisible: "::-p-text(The reflection call timed out before I could ans)",
    frame: true,
  },
  // A session still coming up: the bootstrap narration is the whole view, since there is no
  // transcript yet. The mobile pair is where a long unbroken path would overflow if it could.
  {
    name: "conversation-bootstrap",
    viewport: { width: 1200, height: 900 },
    closeApprovals: true,
    // A line the panel shows only while expanded, which is the state this scene exists to check.
    expectVisible: "::-p-text(Enumerating objects: 4821)",
    frame: true,
  },
  {
    name: "conversation-bootstrap-mobile",
    viewport: { width: 390, height: 900 },
    closeApprovals: true,
    // A line the panel shows only while expanded, which is the state this scene exists to check.
    expectVisible: "::-p-text(Enumerating objects: 4821)",
    frame: true,
  },
  // The other half of the panel: a finished session, where the narration is collapsed to its
  // summary line and the transcript is what the page is for.
  {
    name: "conversation-narration-collapsed",
    viewport: { width: 1200, height: 900 },
    closeApprovals: true,
    expectVisible: "::-p-text(Workspace ready at)",
    frame: true,
  },
  // A prompt the console would not take. The mocked 409 (mock_api.ts) is the interesting half of
  // sending from this surface: the operator's text is still in the box below the notice, because
  // nothing anywhere else has a copy of it.
  {
    name: "conversation-prompt-refused",
    viewport: { width: 1200, height: 900 },
    closeApprovals: true,
    typeInto: { selector: 'textarea[aria-label="Message"]', text: "Did the degraded server recover?" },
    clicks: ["button::-p-text(Send)"],
    expectVisible: "::-p-text(a prompt is already queued)",
    frame: true,
  },
  {
    // The frame inspector, which opens on the tail of the log — prove the response arrived by
    // waiting for its harness-kind label. Native payload details may be folded in compact JSON blocks.
    name: "session-frames",
    viewport: { width: 1200, height: 1000 },
    closeApprovals: true,
    expectVisible: "::-p-text(claude_code)",
    frame: true,
  },
  {
    name: "session-frames-mobile",
    viewport: { width: 390, height: 900 },
    closeApprovals: true,
    expectVisible: "::-p-text(claude_code)",
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
    // Toggling the checkbox fires an async refetch. Without this the scene would pass while
    // showing exactly the plain `history` rows — its whole point unverified — so assert the
    // provenance line only an auto-approved row renders.
    expectVisible: "::-p-text(Auto-approved by unconditional_v1)",
    frame: true,
  },
  {
    // A ledger deeper than one page: the "Load older calls" affordance at the bottom, and the
    // placeholders code_block.tsx leaves where a row's editor is not built yet (an element shot of
    // the whole list captures rows past the viewport, which is exactly where those are).
    name: "history-paged",
    viewport: { width: 1200, height: 900 },
    closeApprovals: true,
    scrollToBottom: ".haku-page-scroll",
    expectVisible: "button::-p-text(Load older calls)",
    frame: true,
  },
  {
    name: "sync-current",
    viewport: { width: 600, height: 420 },
    clicks: ['[aria-label="Up to date"]'],
    expectVisible: '[aria-label="Sync status"]',
  },
  {
    name: "sync-syncing",
    viewport: { width: 600, height: 420 },
    clicks: ['[aria-label="Syncing"]'],
    expectVisible: '[aria-label="Sync status"]',
  },
  {
    name: "sync-error",
    viewport: { width: 600, height: 420 },
    clicks: ['[aria-label="Sync error"]'],
    expectVisible: '[aria-label="Sync status"]',
  },
  {
    name: "session-expiring",
    viewport: { width: 600, height: 420 },
    clicks: ['[aria-label="Session expiring soon"]'],
    expectVisible: '[aria-label="Console session"]',
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
const sceneFailures = [];
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
      scrollToBottom,
      clickTabText,
      typeInto,
    } of SCENES) {
      const page = await browser.newPage();
      // One scene must not hide the rest: a failure is recorded and the sweep continues, so a
      // single run enumerates every broken scene (and every route its mocks are missing).
      try {
        // Matches visual-test-lib.mjs's fixed epoch: harness.tsx's chromeProps feeds the real
        // Date.now() into sampleRecentToolCalls, so without a frozen clock any date-relative text
        // (e.g. formatAge) would drift between runs instead of rendering the same value every time.
        await prepareDeterministicPage(page, { viewport: { ...viewport, deviceScaleFactor: 2 }, colorScheme });
        page.on("console", (message) => console.log(`[${name}] browser: ${message.text()}`));
        page.on("pageerror", (error) => console.error(`[${name}] browser error:`, error));
        await page.setRequestInterception(true);
        const html = pageHtml(css, harnessJs, name, colorScheme);
        // Everything this page may load is served right here; anything else escaping to the network
        // is a hole in the harness's hermeticity and fails the scene by name below — in the test
        // sandbox it could only fail asynchronously, racing the capture into a flaked shot.
        const escapedRequests = [];
        page.on("request", (request) => {
          if (request.isNavigationRequest() && request.frame() === page.mainFrame()) {
            void request.respond({ status: 200, contentType: "text/html", body: html });
          } else if (request.url().startsWith("https://haku-ui.test/")) {
            void request.respond({ status: 200, contentType: "text/html", body: mockHakuUi });
          } else if (/^(?:data|about):/.test(request.url())) {
            // Resolves inside the page — not network, so not a hermeticity hole.
            void request.continue();
          } else {
            escapedRequests.push(`${request.resourceType()} ${request.url()}`);
            void request.abort();
          }
        });
        await page.goto("https://haku-console.test/", { waitUntil: "load" });
        await page.waitForSelector("#app > *", { timeout: 10_000 });
        if (frame) {
          const hakuFrame = page.frames().find((candidate) => candidate.url().startsWith("https://haku-ui.test/"));
          if (!hakuFrame) throw new Error(`scene ${name}: mocked Haku UI iframe did not load`);
          await hakuFrame.waitForSelector("main", { timeout: 10_000 });
        }
        // Wait for the initial fetches and paint rather than guessing how long the first render
        // takes. This also gives approval cards a chance to arm without coupling the harness to
        // their implementation delay.
        await waitForStable(page);
        await assertNetworkSettled(page, { context: `scene ${name}` });
        await page.waitForFunction(
          () =>
            [...document.querySelectorAll("button")]
              .filter((button) => /^(Approve|Deny)$/.test(button.textContent?.trim() ?? ""))
              .every((button) => !button.disabled),
          { timeout: 5_000 }
        );
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
        // A scene whose subject is what a control does with operator input has to supply that input
        // first — a composer's Send stays disabled until something is typed.
        if (typeInto) {
          await page.waitForSelector(typeInto.selector, { visible: true, timeout: 5_000 });
          await page.type(typeInto.selector, typeInto.text);
          await waitForStable(page);
        }
        // Some scenes need clicks to reveal state internal to a component: a popover's open state
        // (location-sharing control) or history rows toggled into their detailed view. Each click
        // re-renders the DOM, so wait for its network and paint state before the next one.
        const sceneClicks = clicks ?? (click ? [click] : []);
        if (clickTabText) {
          await page.evaluate((label) => {
            const tab = [...document.querySelectorAll('[role="tab"]')].find(
              (candidate) => candidate.textContent?.trim() === label
            );
            if (!tab) throw new Error(`No tab found for ${label}`);
            tab.scrollIntoView({ block: "nearest", inline: "nearest" });
            tab.click();
          }, clickTabText);
          await page.waitForFunction(
            (label) =>
              [...document.querySelectorAll('[role="tab"]')].some(
                (candidate) =>
                  candidate.textContent?.trim() === label && candidate.getAttribute("aria-selected") === "true"
              ),
            { timeout: 5_000 },
            clickTabText
          );
          await page.mouse.move(0, 0);
          await waitForStable(page);
          await assertNetworkSettled(page, { context: `scene ${name}` });
        }
        for (const selector of sceneClicks) {
          await page.click(selector);
          await waitForStable(page);
          await assertNetworkSettled(page, { context: `scene ${name}` });
          // `page.click` leaves the cursor on the element it clicked, so any Tooltip attached to it
          // opens and stays open into the capture — in the sync/session scenes that put a tooltip
          // squarely over the panel heading it had just revealed. Park the cursor off-canvas so a
          // scene captures its post-click state, not a hover state nobody asked for.
          await page.mouse.move(0, 0);
          await waitForStable(page);
        }
        // A click that lands on the wrong element fails silently — `page.click` only throws when
        // nothing matches at all — and so does one whose effect arrives asynchronously and never
        // does. Either way the scene renders something plausible and the test passes. So a scene
        // that clicks must also state what the clicks were for; `expectVisible` is its own proof.
        if (expectVisible) {
          await page.waitForSelector(expectVisible, { visible: true, timeout: 5_000 });
        } else if (clicks ?? click ?? clickTabText) {
          throw new Error(`scene ${name}: has clicks but no expectVisible — assert what they reveal`);
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
        // rounds (list, then a per-connection status probe). Waiting for these loaders to clear
        // is a no-op on scenes that never had them: `hidden: true` is already satisfied for a
        // selector that was never in the DOM.
        await page.waitForSelector('[aria-label="Loading MCP servers"]', { hidden: true, timeout: 5_000 });
        await page.waitForSelector('[aria-label="Loading Agents"]', { hidden: true, timeout: 5_000 });
        await page.waitForSelector('[aria-label="Loading Agent enrollment"]', { hidden: true, timeout: 5_000 });
        await page.waitForSelector('[aria-label="Checking connection status"]', { hidden: true, timeout: 5_000 });
        // A scene whose subject is at the end of a long scroller (the history view's "Load older
        // calls") captures the bottom of it. Scrolling is also what mounts the code blocks of the
        // rows down there, since they build their editors only once near the viewport.
        if (scrollToBottom) {
          await page.$eval(scrollToBottom, (element) => {
            element.scrollTop = element.scrollHeight;
          });
          await waitForStable(page);
        }
        // The capture gate: nothing in flight, nothing recorded against the mocks, nothing escaped
        // to the real network, and fonts/images/paint stable — a violation fails the scene naming
        // what happened rather than capturing a plausible-looking error or missing-data state.
        await assertNetworkSettled(page, { context: `scene ${name}` });
        if (escapedRequests.length > 0) {
          throw new Error(`scene ${name}: requests escaped the harness mocks:\n  ${escapedRequests.join("\n  ")}`);
        }
        await waitForStable(page);
        const file = `${name}-${colorScheme}.png`;
        const shot = element
          ? await screenshotElement(page, element, { context: `scene ${name}` })
          : await page.screenshot({ fullPage });
        writeFileSync(join(outDir, file), shot);
        assets.push({ path: file, label: `${name} - ${colorScheme}` });
        console.log(`wrote ${join(outDir, file)}`);
      } catch (error) {
        sceneFailures.push(`${name} (${colorScheme}): ${error.message}`);
      } finally {
        await page.close();
      }
    }
  }
} finally {
  await browser.close();
}
if (sceneFailures.length > 0) {
  throw new Error(`${sceneFailures.length} scene(s) failed:\n  ${sceneFailures.join("\n  ")}`);
}
writeVisualReviewManifest(outDir, {
  title: "Haku Console",
  assets,
});
console.log(`\n${assets.length} screenshots in ${outDir}`);
