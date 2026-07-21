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

import { frozenClockScript, FROZEN_NOW_MS } from "./launcher.mjs";

/** Wait `ms` — the standard settle delay after a load/click/mount before capturing. */
export const settle = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

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
