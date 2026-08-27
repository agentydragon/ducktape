import assert from "node:assert/strict";

import { prepareDeterministicPage, screenshotElement, settle } from "./capture.mjs";

function fakePage() {
  const calls = [];
  return {
    calls,
    evaluateOnNewDocument: async (script) => calls.push(["evaluateOnNewDocument", script]),
    setViewport: async (viewport) => calls.push(["setViewport", viewport]),
    emulateMediaFeatures: async (features) => calls.push(["emulateMediaFeatures", features]),
  };
}

{
  const page = fakePage();
  const viewport = { width: 600, height: 300, deviceScaleFactor: 2 };
  await prepareDeterministicPage(page, { viewport, colorScheme: "dark" });
  const [clockCall, viewportCall, mediaCall] = page.calls;
  assert.equal(clockCall[0], "evaluateOnNewDocument");
  assert.match(clockCall[1], /frozenClock\(\d+\)/);
  assert.deepEqual(viewportCall, ["setViewport", viewport]);
  assert.deepEqual(mediaCall, [
    "emulateMediaFeatures",
    [
      { name: "prefers-reduced-motion", value: "reduce" },
      { name: "prefers-color-scheme", value: "dark" },
    ],
  ]);
}

{
  // A custom nowMs overrides the shared FROZEN_NOW_MS default.
  const page = fakePage();
  await prepareDeterministicPage(page, { viewport: { width: 1, height: 1 }, colorScheme: "light", nowMs: 42 });
  assert.match(page.calls[0][1], /frozenClock\(42\)/);
}

{
  // Found: screenshots the handle and coerces non-Buffer screenshot data to a Buffer.
  const page = { $: async () => ({ screenshot: async () => new Uint8Array([1, 2, 3]) }) };
  const shot = await screenshotElement(page, "#found");
  assert.ok(Buffer.isBuffer(shot));
  assert.deepEqual([...shot], [1, 2, 3]);
}

{
  // Not found: throws a helpful error instead of Puppeteer's opaque null-handle TypeError.
  const page = { $: async () => null };
  await assert.rejects(() => screenshotElement(page, "#missing"), /^Error: element "#missing" not found$/);
  await assert.rejects(
    () => screenshotElement(page, "#missing", { context: "scene foo" }),
    /^Error: scene foo: element "#missing" not found$/
  );
}

{
  // settle yields to the event loop instead of resolving synchronously — that is what
  // callers await it for, to let a pending render flush before a screenshot.
  //
  // Asserted as ordering rather than elapsed time on purpose. `Date.now() - started >= 10`
  // tests the platform's timer, not this wrapper, and flakes: setTimeout may fire a hair
  // early against Date.now()'s millisecond resolution, which failed CI on ad9239cb.
  let resolved = false;
  const pending = settle(0).then(() => {
    resolved = true;
  });
  assert.equal(resolved, false);
  await pending;
  assert.equal(resolved, true);
}

console.log("capture.test.mjs passed");
