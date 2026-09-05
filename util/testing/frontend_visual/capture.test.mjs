import assert from "node:assert/strict";

import {
  abortUnexpectedRequests,
  assertNetworkSettled,
  prepareDeterministicPage,
  screenshotElement,
} from "./capture.mjs";

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
  // The fence continues allowed requests, and aborts + records everything else by type and URL.
  const handlers = {};
  const seen = [];
  const page = {
    setRequestInterception: async (on) => seen.push(["intercept", on]),
    on: (event, handler) => {
      handlers[event] = handler;
    },
  };
  const request = (url, resourceType, log) => ({
    url: () => url,
    resourceType: () => resourceType,
    continue: async () => log.push(`continue ${url}`),
    abort: async () => log.push(`abort ${url}`),
  });
  const violations = await abortUnexpectedRequests(page, (candidate) => candidate.url().startsWith("file://"));
  assert.deepEqual(seen, [["intercept", true]]);
  const log = [];
  handlers.request(request("file:///harness/index.html", "document", log));
  handlers.request(request("https://fonts.example/inter.woff2", "font", log));
  assert.deepEqual(log, ["continue file:///harness/index.html", "abort https://fonts.example/inter.woff2"]);
  assert.deepEqual(violations, ["font https://fonts.example/inter.woff2"]);
}

{
  // A page with no ledger has nothing to settle: no waiting, no violation read.
  const evaluated = [];
  const page = {
    evaluate: async (fn) => {
      evaluated.push(fn);
      return false;
    },
    waitForFunction: async () => {
      throw new Error("must not wait on a page with no ledger");
    },
  };
  await assertNetworkSettled(page, { context: "scene x" });
  assert.equal(evaluated.length, 1);
}

console.log("capture.test.mjs passed");
