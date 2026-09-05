/**
 * Deterministic-screenshot primitives shared by every visual-test consumer.
 *
 * `launcher.mjs` launches the browser; these prepare and capture a single already-created page.
 * Loading content into the page stays caller-owned — it differs too much per harness (a `file://`
 * harness page, an inlined HTML string, a mocked-iframe navigation via request interception) to
 * abstract usefully; forcing it in here would just relocate each harness's own config-flag
 * sprawl into a shared file without reducing it. Callers also own their own loop/orchestration
 * (one scenario per process vs. many scenes per browser) and their own exit code — nothing here
 * calls `process.exit()`.
 */

// `document`, `window`, and `requestAnimationFrame` are the browser page's, referenced inside
// page.evaluate callbacks (which run in the headless page, not Node) — declare them so this
// Node script lints under the .mjs node-globals block.
/* global document, window, requestAnimationFrame */

import { frozenClockScript, FROZEN_NOW_MS } from "./launcher.mjs";

/**
 * The bound every wait in a visual test takes — navigations, the mount wait, a scenario's own
 * condition, the network-settle gate.
 *
 * The number is measured, not guessed: across a 49-target parallel `--nocache_test_results` run,
 * the slowest navigation to networkidle0 was 1.9s and the slowest mount after it 79ms, so this is
 * ~380x the slowest healthy mount seen under the load that produces flakes. That headroom is the
 * whole point — the 5s literal this replaces was already 60x the healthy mount and a starved RBE
 * worker still outran it (debug/2026_08_rbe_small_test_timeouts.md).
 *
 * It is not larger because a bound only helps while it is the thing that reports the failure: the
 * smallest scenario using it is a `small` (60s) target, which must fail here — naming the selector
 * it waited for — rather than be killed by Bazel, and Puppeteer caps any single CDP call at 180s
 * regardless.
 */
export const WAIT_TIMEOUT_MS = 30_000;

/**
 * Wait until the page is done rendering what it has: fonts applied, images decoded, a frame
 * painted. What a fixed delay after mount was guessing at.
 *
 * Finishes as soon as those are true rather than always costing the delay, and — the reason it
 * exists — cannot pass early on a loaded runner the way a sleep can.
 *
 * Deliberately does not await `document.getAnimations()`. `DISABLE_ANIMATIONS_CSS` pins every
 * animation with `animation-play-state: paused`, and a paused animation's `finished` never
 * settles, so awaiting it would hang instead of capturing. Animations are already deterministic
 * by the time this runs.
 *
 * This cannot know a scene's *own* readiness — data arriving, a component mounting lazily. Wait
 * for that with `page.waitForSelector`/`waitForFunction` on the thing the scene is about, then
 * call this.
 */
export async function waitForStable(page) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all(
      Array.from(document.images)
        .filter((image) => !image.complete)
        // A broken src rejects; that is the page's problem to render, not ours to wait on.
        .map((image) => image.decode().catch(() => {}))
    );
    // Two frames: the first flushes pending style and layout, the second lands after paint.
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
}

/**
 * Freeze `page`'s clock and set its viewport/media emulation. Call before loading content —
 * `evaluateOnNewDocument` only takes effect on documents loaded after it's registered.
 */
export async function prepareDeterministicPage(page, { viewport, colorScheme, nowMs = FROZEN_NOW_MS }) {
  await page.evaluateOnNewDocument(frozenClockScript(nowMs));
  await page.setViewport(viewport);
  await page.emulateMediaFeatures([
    { name: "prefers-reduced-motion", value: "reduce" },
    { name: "prefers-color-scheme", value: colorScheme },
  ]);
}

/**
 * Wait until the harness's stubbed network is quiet, then fail on anything it recorded.
 *
 * The in-page half is `window.__visualNetworkLedger__`
 * (haku/console/frontend/tool_rendering/screenshot/visual_network_ledger.ts), maintained by the
 * harness's fetch stub: `pending` holds each stubbed fetch's URL while it is in flight, and
 * `violations` records what must fail the run — an unmatched route, a rejected fetch, an
 * unhandled promise rejection. Quiet means `pending` stayed empty across a painted frame, so a
 * response whose re-render immediately starts another fetch is drained rather than captured
 * between the two. A timeout names what was still in flight instead of reporting a bare timeout.
 *
 * A page with no ledger passes: a harness that stubs no fetch has nothing to settle, and
 * `abortUnexpectedRequests` is the fence that keeps such a page's network empty.
 */
export async function assertNetworkSettled(page, { context, timeoutMs = WAIT_TIMEOUT_MS } = {}) {
  const prefix = context ? `${context}: ` : "";
  if (!(await page.evaluate(() => Boolean(window.__visualNetworkLedger__)))) return;
  try {
    await page.waitForFunction(
      async () => {
        const ledger = window.__visualNetworkLedger__;
        if (ledger.pending.length > 0) return false;
        await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        return ledger.pending.length === 0;
      },
      { timeout: timeoutMs }
    );
  } catch (error) {
    const pending = await page.evaluate(() => window.__visualNetworkLedger__.pending);
    if (pending.length > 0) {
      throw new Error(`${prefix}requests still in flight after ${timeoutMs}ms: ${pending.join(", ")}`);
    }
    throw error;
  }
  const violations = await page.evaluate(() => window.__visualNetworkLedger__.violations);
  if (violations.length > 0) {
    throw new Error(`${prefix}network violations:\n  ${violations.join("\n  ")}`);
  }
}

/**
 * Fence off the real network: abort and record every request `allow` does not accept.
 *
 * For a page whose content is entirely local (inlined HTML, `file://` assets, stubbed fetch),
 * any escaping request is a hole in its hermeticity. In the test sandbox such a request can only
 * fail, and its failure racing the capture is exactly how a transient error state gets published
 * as a plausible-looking baseline — so the fence aborts it immediately and the caller fails the
 * scene by asserting the returned array is empty before capturing.
 *
 * Installs request interception; `allow` receives the Puppeteer request object.
 */
export async function abortUnexpectedRequests(page, allow) {
  await page.setRequestInterception(true);
  const violations = [];
  page.on("request", (request) => {
    // data:/about: resolve inside the page — not network, so never a hermeticity hole.
    if (/^(?:data|about):/.test(request.url()) || allow(request)) {
      void request.continue();
      return;
    }
    violations.push(`${request.resourceType()} ${request.url()}`);
    void request.abort();
  });
  return violations;
}

/**
 * Screenshot the element matching `selector`; throws (prefixed with `context`, if given) instead
 * of the opaque "Cannot read properties of null" that calling `.screenshot()` on a missing
 * element's `null` handle would otherwise produce.
 */
export async function screenshotElement(page, selector, { context } = {}) {
  const handle = await page.$(selector);
  if (!handle) throw new Error(`${context ? `${context}: ` : ""}element ${JSON.stringify(selector)} not found`);
  const data = await handle.screenshot();
  return Buffer.isBuffer(data) ? data : Buffer.from(data);
}
