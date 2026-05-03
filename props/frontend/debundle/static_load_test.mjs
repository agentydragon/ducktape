#!/usr/bin/env node
// Self-hosted live-browser smoke for the props/frontend debundle harness.
//
// Unlike the gaffer Tana smoke (which MITM-proxies a real upstream host),
// this smoke is fully self-hosted: a tiny Node http server serves the
// debundler's harness output tree, then a headless Chromium drives it
// through puppeteer and asserts on:
//   1. no failed requests under the harness asset path
//   2. no console errors / page errors
//   3. the navbar selector is visible
//   4. clicking a nav link navigates (URL hash + DOM update)
//
// The harness is a self-contained tree: index.html, bootstrap.js, the
// transformed chunks, and (via the swap_vendor_chunks stage) generated
// vendor wrappers + a vendor manifest. Vendor chunks resolve back to
// the package roots passed via `--package-root` flags (same shape as
// the live-proxy CLI).

import { createReadStream, existsSync, statSync, readFileSync } from "node:fs";
import { extname, join, normalize, resolve } from "node:path";
import { createServer } from "node:net";
import { createServer as createHttpServer } from "node:http";

import { launchPuppeteerBrowser } from "../../../util/testing/frontend_visual/puppeteer-lib.mjs";

function parseArgs(argv) {
  const options = {
    appManifestPath: null,
    packageRoots: {},
    waitForSelector: null,
    waitTimeoutMs: 30_000,
    gotoTimeoutMs: 30_000,
    waitForNavInteraction: false,
  };
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index];
    switch (arg) {
      case "--app-manifest":
        options.appManifestPath = requireValue(argv, ++index, arg);
        break;
      case "--package-root": {
        const value = requireValue(argv, ++index, arg);
        const eq = value.indexOf("=");
        if (eq <= 0) throw new Error(`--package-root must be <pkg>=<dir>, got ${value}`);
        options.packageRoots[value.slice(0, eq)] = value.slice(eq + 1);
        break;
      }
      case "--wait-for-selector":
        options.waitForSelector = requireValue(argv, ++index, arg);
        break;
      case "--wait-timeout-ms":
        options.waitTimeoutMs = parseInt(requireValue(argv, ++index, arg), 10);
        break;
      case "--goto-timeout-ms":
        options.gotoTimeoutMs = parseInt(requireValue(argv, ++index, arg), 10);
        break;
      case "--assert-nav-interaction":
        options.waitForNavInteraction = true;
        break;
      default:
        throw new Error(`Unknown argument: ${arg}`);
    }
  }
  if (!options.appManifestPath) throw new Error("--app-manifest is required");
  return options;
}

function requireValue(argv, index, flag) {
  const value = argv[index];
  if (value === undefined) throw new Error(`${flag} requires a value`);
  return value;
}

function allocatePort() {
  return new Promise((res, rej) => {
    const s = createServer();
    s.once("error", rej);
    s.listen(0, "127.0.0.1", () => {
      const port = s.address().port;
      s.close((err) => (err ? rej(err) : res(port)));
    });
  });
}

const CONTENT_TYPES = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".txt": "text/plain; charset=utf-8",
};

function safeJoin(root, relative) {
  const normalized = normalize(relative).replace(/^([/\\])+/, "");
  const resolved = resolve(root, normalized);
  const rootResolved = resolve(root);
  if (!resolved.startsWith(`${rootResolved}/`) && resolved !== rootResolved) {
    throw new Error(`refusing to serve outside root: ${relative}`);
  }
  return resolved;
}

function startStaticServer({ appRoot, vendorIndex }) {
  const server = createHttpServer((req, res) => {
    try {
      const url = new URL(req.url ?? "/", "http://_local");
      let path = url.pathname;
      if (path === "/") path = "/index.html";
      // Vendor chunk: served from the resolved package subpath (same
      // shape the live proxy uses).
      const pathNoLeading = path.replace(/^[/\\]+/, "");
      const vendorMatch = vendorIndex.find((entry) => pathNoLeading.startsWith(`${entry.chunkId}/`));
      if (vendorMatch) {
        const suffix = pathNoLeading.slice(vendorMatch.chunkId.length + 1);
        const filePath =
          vendorMatch.entryFile === suffix ? vendorMatch.filePath : safeJoin(vendorMatch.mountRoot, suffix);
        if (existsSync(filePath) && statSync(filePath).isFile()) {
          res.setHeader("cache-control", "no-store");
          res.setHeader("content-type", CONTENT_TYPES[extname(filePath)] ?? "application/octet-stream");
          res.writeHead(200);
          createReadStream(filePath).pipe(res);
          return;
        }
      }
      const filePath = safeJoin(appRoot, pathNoLeading);
      if (existsSync(filePath) && statSync(filePath).isFile()) {
        res.setHeader("cache-control", "no-store");
        res.setHeader("content-type", CONTENT_TYPES[extname(filePath)] ?? "application/octet-stream");
        res.writeHead(200);
        createReadStream(filePath).pipe(res);
        return;
      }
      res.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
      res.end(`not found: ${path}\n`);
    } catch (error) {
      res.writeHead(500, { "content-type": "text/plain; charset=utf-8" });
      res.end(`${error?.stack ?? error}\n`);
    }
  });
  return server;
}

function loadVendorIndex({ vendorManifestPath, packageRoots }) {
  if (!existsSync(vendorManifestPath)) return [];
  const manifest = JSON.parse(readFileSync(vendorManifestPath, "utf8"));
  if (manifest.kind !== "js.vendor_resolution_manifest") {
    throw new Error(`Unexpected vendor manifest kind: ${manifest.kind}`);
  }
  const manifestDir = resolve(vendorManifestPath, "..");
  const entries = [];
  for (const [chunkPathKey, entry] of Object.entries(manifest.resolutions ?? {})) {
    const chunkPath = entry.chunkPath ?? chunkPathKey;
    const chunkId = chunkPath.endsWith(".js") ? chunkPath.slice(0, -3) : chunkPath;
    const wrapperPath = entry.generatedWrapperPath ? resolve(manifestDir, entry.generatedWrapperPath) : null;
    const packageRoot = packageRoots[entry.package];
    if (!packageRoot) {
      throw new Error(`vendor package root not provided for ${entry.package}`);
    }
    const filePath = wrapperPath ?? resolve(packageRoot, entry.subpath);
    const mountRoot = wrapperPath ? resolve(wrapperPath, "..") : packageRoot;
    entries.push({
      chunkId,
      entryFile: entry.entryFile,
      filePath,
      mountRoot,
    });
  }
  return entries;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  // Resolve the manifest via the JS_BINARY runfiles env, mirroring
  // the live_proxy entry point.
  const manifestRel = options.appManifestPath;
  const manifest = JSON.parse(readFileSync(manifestRel, "utf8"));
  const manifestDir = resolve(manifestRel, "..");
  const appRoot = resolve(manifestDir, manifest.outDir);
  const vendorManifestPath = manifest.vendorManifestPath
    ? resolve(manifestDir, manifest.vendorManifestPath)
    : join(appRoot, "vendors", "manifest.json");
  const vendorIndex = loadVendorIndex({ vendorManifestPath, packageRoots: options.packageRoots });

  const port = await allocatePort();
  const server = startStaticServer({ appRoot, vendorIndex });
  await new Promise((r) => server.listen(port, "127.0.0.1", r));

  const baseUrl = `http://127.0.0.1:${port}`;
  const browser = await launchPuppeteerBrowser({
    args: ["--disable-dev-shm-usage", "--disable-setuid-sandbox", "--no-sandbox"],
    headless: true,
  });

  const page = await browser.newPage();
  const consoleMessages = [];
  const failedRequests = [];
  const pageErrors = [];

  page.on("console", (m) => consoleMessages.push(`[${m.type()}] ${m.text()}`));
  page.on("pageerror", (e) => pageErrors.push(e.stack ?? e.message));
  page.on("requestfailed", (req) =>
    failedRequests.push({ url: req.url(), errorText: req.failure()?.errorText ?? "unknown" })
  );

  let assertionError = null;
  try {
    await page.goto(`${baseUrl}/`, { timeout: options.gotoTimeoutMs, waitUntil: "domcontentloaded" });
    if (options.waitForSelector) {
      await page.waitForSelector(options.waitForSelector, { timeout: options.waitTimeoutMs });
    }
    await new Promise((r) => setTimeout(r, 500));

    const pageState = await page.evaluate(() => ({
      title: document.title,
      hash: location.hash,
      navText: document.querySelector("nav")?.innerText ?? null,
      headerText: document.querySelector("header h1")?.innerText ?? null,
    }));

    // Assertion 1: no failed asset requests.
    if (failedRequests.length > 0) {
      throw new Error(`failed requests:\n${JSON.stringify(failedRequests, null, 2)}`);
    }

    // Assertion 2: no console errors.
    const consoleErrors = consoleMessages.filter((m) => m.startsWith("[error]"));
    if (consoleErrors.length > 0 || pageErrors.length > 0) {
      throw new Error(
        `console/page errors:\n${[...consoleErrors, ...pageErrors].join("\n")}\n\nall console:\n${consoleMessages.join("\n")}`
      );
    }

    // Assertion 3: navbar selector visible.
    if (!pageState.navText) {
      throw new Error(`expected <nav> with text content, got ${JSON.stringify(pageState)}`);
    }
    if (!pageState.headerText || !pageState.headerText.includes("Props")) {
      throw new Error(`expected <h1>Props</h1>, got headerText=${JSON.stringify(pageState.headerText)}`);
    }

    // Assertion 4: clicking a nav link updates the URL hash + DOM.
    if (options.waitForNavInteraction) {
      await page.click('nav a[href*="/runs"]');
      await page.waitForFunction(() => location.hash === "#/runs", { timeout: options.waitTimeoutMs });
      const updatedPathnameText = await page.evaluate(
        () => document.querySelector('[data-debundle-smoke="pathname"]')?.textContent ?? null
      );
      if (updatedPathnameText !== "/runs") {
        throw new Error(
          `nav click did not update DOM pathname; got ${JSON.stringify(updatedPathnameText)}, expected "/runs"`
        );
      }
    }
  } catch (error) {
    assertionError = error;
  } finally {
    await browser.close();
    await new Promise((r) => server.close(r));
  }
  if (assertionError) {
    throw assertionError;
  }
}

await main();
