import assert from "node:assert/strict";
import { cpSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";

import { launchPuppeteerBrowser } from "../../../../util/testing/frontend_visual/puppeteer-lib.mjs";
import { runMockBrowserBundlePipeline } from "./mock_pipeline.mjs";

async function captureState(appRoot) {
  const browser = await launchPuppeteerBrowser({
    args: ["--allow-file-access-from-files", "--disable-dev-shm-usage", "--no-sandbox", "--disable-setuid-sandbox"],
    headless: true,
  });
  const page = await browser.newPage();
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.stack ?? error.message));
  try {
    await page.goto(pathToFileURL(join(appRoot, "index.html")).href, { waitUntil: "networkidle0" });
    await page.waitForFunction(() => globalThis.__mockBundleState?.lazy != null, { timeout: 10_000 });
    const state = await page.evaluate(() => globalThis.__mockBundleState);
    assert.deepEqual(pageErrors, []);
    return state;
  } finally {
    await browser.close();
  }
}

function buildRustApp(outRoot) {
  const runfiles = process.env.TEST_SRCDIR;
  const workspace = process.env.TEST_WORKSPACE;
  assert.ok(runfiles && workspace, "bazel runfiles env missing");
  const root = join(runfiles, workspace);
  const fixtureRoot = join(root, "devinfra/js/debundle/harness/testdata/mock_browser_bundle/generated");
  const rustBin = join(root, process.env.RUST_DEBUNDLE_BIN);
  const run = spawnSync(
    rustBin,
    ["--input-root", join(fixtureRoot, "snapshot"), "--js-list", join(fixtureRoot, "extracted/js-files.txt"), "--out-root", outRoot],
    { encoding: "utf8" }
  );
  assert.equal(run.status, 0, `debundle_rust failed:\nstdout:\n${run.stdout}\nstderr:\n${run.stderr}`);
  let html = readFileSync(join(fixtureRoot, "snapshot/index.html"), "utf8");
  html = html.replaceAll('"/static/', '"./static/').replaceAll('"/preload/', '"./preload/');
  writeFileSync(join(outRoot, "index.html"), html);
  cpSync(join(fixtureRoot, "snapshot/preload"), join(outRoot, "preload"), { recursive: true });
}

async function buildJsApp(outRoot) {
  const { appRoot } = await runMockBrowserBundlePipeline({ prefix: "debundle-pipeline-impl-js-" });
  cpSync(appRoot, outRoot, { recursive: true });
}

const impl = process.env.DEBUNDLE_IMPL;
if (impl === "js" || impl === "rust") {
  const outRoot = mkdtempSync(join(tmpdir(), `debundle-impl-e2e-${impl}-`));
  if (impl === "js") {
    await buildJsApp(outRoot);
  } else {
    buildRustApp(outRoot);
  }
  const state = await captureState(outRoot);
  assert.equal(state.summary.headline, "Ada Lovelace:11");
  assert.equal(state.chip.text, "chip:mock-dashboard@7");
} else if (impl === "both") {
  const jsOut = mkdtempSync(join(tmpdir(), "debundle-impl-e2e-js-"));
  const rustOut = mkdtempSync(join(tmpdir(), "debundle-impl-e2e-rust-"));
  await buildJsApp(jsOut);
  buildRustApp(rustOut);
  const [jsState, rustState] = await Promise.all([captureState(jsOut), captureState(rustOut)]);
  assert.deepEqual(rustState, jsState);
} else {
  throw new Error(`Unknown DEBUNDLE_IMPL=${impl}`);
}
