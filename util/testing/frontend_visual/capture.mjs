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

// `document` and `requestAnimationFrame` are the browser page's, referenced inside
// page.evaluate callbacks (which run in the headless page, not Node) — declare them so this
// Node script lints under the .mjs node-globals block.
/* global document, requestAnimationFrame */

import { frozenClockScript, FROZEN_NOW_MS } from "./launcher.mjs";

/** Wait `ms`. Prefer `waitForStable` or a Puppeteer `waitFor*` — see STYLE.md § Waiting. */
export const settle = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

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
