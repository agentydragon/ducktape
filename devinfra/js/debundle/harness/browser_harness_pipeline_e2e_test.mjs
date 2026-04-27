import assert from "node:assert/strict";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import test from "node:test";

import { launchPuppeteerBrowser } from "../../../../util/testing/frontend_visual/puppeteer-lib.mjs";
import { readUtf8 } from "../test_support/fixture_lib.mjs";
import { runMockBrowserBundlePipeline } from "./mock_browser_bundle_pipeline_lib.mjs";

test("generic mock bundle runs through normalize+rename pipeline and passes a browser harness check", async () => {
  const { appRoot, result } = await runMockBrowserBundlePipeline({
    prefix: "debundle-browser-harness-pipeline-e2e-",
  });
  assert.deepEqual(
    result.steps.map((step) => step.operation),
    [
      "load_js_chunks",
      "compute_js_asts",
      "normalize_js_chunks",
      "rename_bindings",
      "rewrite_chunk_entry_specifiers",
      "write_js_tree",
      "emit_browser_harness",
    ]
  );

  const indexHtml = readUtf8(join(appRoot, "index.html"));
  assert.match(indexHtml, /Generated local harness/);
  assert.match(indexHtml, /href="\.\/static\/ActivityPanel-DuckMock\/entry\.js"/);
  assert.match(indexHtml, /href="\.\/static\/SummaryChip-DuckMock\/entry\.js"/);
  assert.match(indexHtml, /href="\.\/static\/chunk-DuckMock\/entry\.js"/);

  const browser = await launchBrowser();
  const page = await browser.newPage();
  const consoleMessages = [];
  const pageErrors = [];
  page.on("console", (message) => consoleMessages.push(message.text()));
  page.on("pageerror", (error) => pageErrors.push(error.stack ?? error.message));

  try {
    await page.goto(pathToFileURL(join(appRoot, "index.html")).href, { waitUntil: "networkidle0" });
    await page.waitForFunction(() => globalThis.__mockBundleState?.lazy != null, { timeout: 10_000 });

    const [appText, appBundle, chipText, statusText, state, harnessState] = await Promise.all([
      page.$eval("#app", (node) => node.textContent),
      page.$eval("#app", (node) => node.dataset.bundle),
      page.$eval("#chip", (node) => node.textContent),
      page.$eval("#status", (node) => node.textContent),
      page.evaluate(() => globalThis.__mockBundleState),
      page.evaluate(() => globalThis.__debundleHarness),
    ]);

    assert.equal(
      harnessState.errors.length,
      0,
      `harness errors:\n${JSON.stringify(harnessState.errors, null, 2)}\nconsole:\n${consoleMessages.join("\n")}`
    );
    assert.deepEqual(pageErrors, [], `page errors:\n${pageErrors.join("\n")}\nconsole:\n${consoleMessages.join("\n")}`);
    assert.deepEqual(state, {
      chip: {
        text: "chip:mock-dashboard@7",
      },
      lazy: {
        badge: "Ada Lovelace:11",
        stamp: "mock-dashboard@7",
        tags: "analysis,dom",
      },
      model: {
        profileName: "Ada Lovelace",
        tags: ["analysis", "dom"],
        total: 11,
      },
      summary: {
        headline: "Ada Lovelace:11",
        stamp: "mock-dashboard@7",
        tags: "analysis|dom",
        total: 11,
      },
    });
    assert.equal(appBundle, "mock-app");
    assert.equal(appText, JSON.stringify(state.summary));
    assert.equal(chipText, "chip:mock-dashboard@7");
    assert.equal(statusText, "Ada Lovelace:11");
  } finally {
    await browser.close();
  }
});

async function launchBrowser() {
  return launchPuppeteerBrowser({
    args: [
      "--allow-file-access-from-files",
      "--disable-dev-shm-usage",
      "--no-sandbox",
      "--disable-setuid-sandbox",
    ],
    headless: true,
  });
}
