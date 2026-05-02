import assert from "node:assert/strict";
import { join } from "node:path";
import test from "node:test";

import { writeJsonFile, writeTextFile } from "./parser_options.mjs";
import { createWebFixtureRoots, readUtf8 } from "./test_helpers.mjs";
import {
  isTargetDocumentRequest,
  loadLiveProxyConfiguration,
  mapLocalAssetPath,
  parseLiveProxyArgs,
  rewriteHtmlForLiveProxy,
} from "./proxy.mjs";

function writeBaseLiveProxyFixture(
  prefix,
  { appRelativeOutDir = "app", sourceBaseUrl = null, uiVersion = "example", vendorManifestPath = null } = {}
) {
  const roots = createWebFixtureRoots(prefix);
  const { packagesRoot, root, sourceRoot } = roots;
  const appRoot = join(root, appRelativeOutDir);
  const vendorsRoot = join(appRoot, "vendors");
  const assetSummaryPath = join(root, "asset-summary.json");
  const sourceHtmlPath = join(sourceRoot, "index.html");
  const appManifestPath = join(appRoot, "manifest.json");

  writeTextFile(
    sourceHtmlPath,
    [
      "<!doctype html>",
      "<html>",
      "  <head>",
      '    <link href="/preload/style.css" rel="stylesheet" />',
      '    <script type="module" crossorigin src="/static/index-Example.js"></script>',
      '    <link rel="modulepreload" crossorigin href="/static/vendor-Example.js">',
      '    <link rel="stylesheet" crossorigin href="/static/index.css">',
      "  </head>",
      "  <body>",
      '    <div id="app"></div>',
      "  </body>",
      "</html>",
      "",
    ].join("\n")
  );
  writeJsonFile(assetSummaryPath, { baseUrl: "https://example.test" });
  writeTextFile(join(appRoot, "bootstrap.js"), 'import "./static/index-Example/runtime.js";\n');
  writeTextFile(join(appRoot, "static", "index-Example", "runtime.js"), "console.log('runtime');\n");
  writeJsonFile(appManifestPath, {
    assetSummaryPath,
    outDir: appRoot,
    sourceHtml: sourceHtmlPath,
    uiVersion,
    ...(vendorManifestPath ? { vendorManifestPath } : {}),
  });
  if (sourceBaseUrl) {
    writeJsonFile(join(appRoot, "SOURCE.json"), {
      baseUrl: sourceBaseUrl,
      uiVersion,
    });
  }

  return {
    appManifestPath,
    appRoot,
    assetSummaryPath,
    packagesRoot,
    root,
    sourceHtmlPath,
    vendorsRoot,
  };
}

test("parseLiveProxyArgs resolves manifest path and numeric ports", () => {
  const { appManifestPath, packagesRoot } = writeBaseLiveProxyFixture("debundle-live-proxy-args-");
  const options = parseLiveProxyArgs([
    "--app-manifest",
    appManifestPath,
    "--package-root",
    `katex=${packagesRoot}/katex`,
    "--proxy-port",
    "9001",
  ]);
  assert.equal(options.appManifestPath, appManifestPath);
  assert.deepEqual(options.packageRoots, { katex: `${packagesRoot}/katex` });
  assert.equal(options.proxyPort, 9001);
});

test("loadLiveProxyConfiguration rewrites the app shell to load the generated bootstrap", () => {
  const { appManifestPath, root, sourceHtmlPath } = writeBaseLiveProxyFixture("debundle-live-proxy-config-");
  const config = loadLiveProxyConfiguration({
    appManifestPath,
    assetHost: "127.0.0.1",
    assetPort: 9900,
    proxyHost: "127.0.0.1",
    proxyPort: 9800,
    stateDir: join(root, "state"),
  });

  assert.equal(config.targetOrigin, "https://example.test");
  assert.equal(config.bootstrapUrl, "/_debundle/live/example/app/bootstrap.js");
  assert.equal(config.controlPaths.liveIndex, "/_debundle/live/example/live-index.html");
  assert.ok(config.injectedHtml.includes('src="/_debundle/live/example/app/bootstrap.js"'));
  assert.ok(config.injectedHtml.includes("js-debundle-live-proxy"));

  const rewritten = rewriteHtmlForLiveProxy(readUtf8(sourceHtmlPath), {
    bootstrapUrl: "/_debundle/live/example/app/bootstrap.js",
    uiVersion: "example",
  });
  assert.ok(rewritten.includes('src="/_debundle/live/example/app/bootstrap.js"'));
  assert.ok(!rewritten.includes("/static/index-Example.js"));
  assert.ok(!rewritten.includes("/static/vendor-Example.js"));
  assert.ok(rewritten.includes("/static/index.css"));
});

test("loadLiveProxyConfiguration falls back to SOURCE.json for the target base URL", () => {
  const fixture = writeBaseLiveProxyFixture("debundle-live-proxy-source-base-url-", {
    sourceBaseUrl: "https://app.example.com",
    uiVersion: "source-fallback",
  });
  writeJsonFile(fixture.assetSummaryPath, {});

  const config = loadLiveProxyConfiguration({
    appManifestPath: fixture.appManifestPath,
    assetHost: "127.0.0.1",
    assetPort: 9903,
    proxyHost: "127.0.0.1",
    proxyPort: 9803,
    stateDir: join(fixture.root, "state"),
  });

  assert.equal(config.targetOrigin, "https://app.example.com");
  assert.equal(config.targetUrl, "https://app.example.com/");
  assert.equal(config.bootstrapUrl, "/_debundle/live/source-fallback/app/bootstrap.js");
});

test("loadLiveProxyConfiguration resolves manifest-dir-relative paths from an absolute manifest location", () => {
  const fixture = writeBaseLiveProxyFixture("debundle-live-proxy-runfiles-manifest-", {
    sourceBaseUrl: "https://app.example.com",
    uiVersion: "runfiles",
  });
  writeJsonFile(fixture.assetSummaryPath, {});
  // The pipeline emits paths relative to the manifest's own directory.
  // appManifest sits at <root>/app/manifest.json; outDir is the manifest's
  // own dir, asset-summary.json is the parent dir, sourceHtml is in the
  // sibling source/ tree.
  writeJsonFile(fixture.appManifestPath, {
    assetSummaryPath: "../asset-summary.json",
    outDir: ".",
    sourceHtml: "../source/index.html",
    uiVersion: "runfiles",
  });

  const config = loadLiveProxyConfiguration({
    appManifestPath: fixture.appManifestPath,
    assetHost: "127.0.0.1",
    assetPort: 9904,
    proxyHost: "127.0.0.1",
    proxyPort: 9804,
    stateDir: join(fixture.root, "state"),
  });

  assert.equal(config.assetSummaryPath, fixture.assetSummaryPath);
  assert.equal(config.sourceHtmlPath, fixture.sourceHtmlPath);
  assert.equal(config.appRoot, fixture.appRoot);
  assert.equal(config.outRoot, fixture.appRoot);
  assert.equal(config.targetOrigin, "https://app.example.com");
});

test("loadLiveProxyConfiguration treats outDir as the app root even when it is not named app", () => {
  const fixture = writeBaseLiveProxyFixture("debundle-live-proxy-versioned-app-root-", {
    appRelativeOutDir: "out/v-example",
    uiVersion: "versioned",
  });

  const config = loadLiveProxyConfiguration({
    appManifestPath: fixture.appManifestPath,
    assetHost: "127.0.0.1",
    assetPort: 9905,
    proxyHost: "127.0.0.1",
    proxyPort: 9805,
    stateDir: join(fixture.root, "state"),
  });

  assert.equal(config.appRoot, fixture.appRoot);
  assert.equal(config.outRoot, fixture.appRoot);
  const fileMapping = mapLocalAssetPath("/_debundle/live/versioned/app/bootstrap.js", config);
  assert.equal(fileMapping.filePath, join(fixture.appRoot, "bootstrap.js"));
});

test("rewriteHtmlForLiveProxy retargets snapshot-asset paths at the internal prefix", () => {
  // Source HTML references the upstream's static/preload paths directly.
  // Without rewriting, the browser fetches them from the live target host
  // — where the snapshot-pinned hashes may have rotated and the request
  // 504s. The rewrite re-anchors them at `appAssetPrefix` so the proxy
  // serves them from the harness tree's mirrored snapshot.
  const sourceHtml = [
    "<!doctype html>",
    "<html><head>",
    '  <link href="/preload/style.css" rel="stylesheet" />',
    '  <link rel="stylesheet" crossorigin href="/static/index-Example.css">',
    '  <link rel="icon" href="/favicon.ico">',
    '  <script type="module" crossorigin src="/static/index-Example.js"></script>',
    '</head><body><div id="app"></div></body></html>',
  ].join("\n");
  const rewritten = rewriteHtmlForLiveProxy(sourceHtml, {
    appAssetPrefix: "/_debundle/live/example/app",
    bootstrapUrl: "/_debundle/live/example/app/bootstrap.js",
    uiVersion: "example",
  });
  // Snapshot-asset prefixes (`/preload/`, `/static/`) get retargeted.
  assert.ok(rewritten.includes('href="/_debundle/live/example/app/preload/style.css"'));
  assert.ok(rewritten.includes('href="/_debundle/live/example/app/static/index-Example.css"'));
  // Original absolute paths to those prefixes should be gone.
  assert.ok(!rewritten.includes('href="/preload/style.css"'));
  assert.ok(!rewritten.includes('href="/static/index-Example.css"'));
  // Out-of-scope absolute paths (`/favicon.ico`) stay as-is so they
  // forward to upstream where they live.
  assert.ok(rewritten.includes('href="/favicon.ico"'));
});

test("isTargetDocumentRequest recognizes top-level HTML navigations", () => {
  const config = {
    targetHost: "example.test",
  };

  assert.equal(
    isTargetDocumentRequest(
      {
        headers: {
          accept: "text/html,application/xhtml+xml",
          host: "example.test",
          "sec-fetch-dest": "document",
        },
        method: "GET",
      },
      config
    ),
    true
  );
  assert.equal(
    isTargetDocumentRequest(
      {
        headers: {
          accept: "application/json",
          host: "api.example.test",
          "sec-fetch-dest": "empty",
        },
        method: "GET",
      },
      config
    ),
    false
  );
});

test("mapLocalAssetPath serves bootstrap, live index, and service worker from the generated tree", () => {
  const { appManifestPath, root } = writeBaseLiveProxyFixture("debundle-live-proxy-assets-");
  const config = loadLiveProxyConfiguration({
    appManifestPath,
    assetHost: "127.0.0.1",
    assetPort: 9900,
    proxyHost: "127.0.0.1",
    proxyPort: 9800,
    stateDir: join(root, "state"),
  });

  const fileMapping = mapLocalAssetPath("/_debundle/live/example/app/bootstrap.js", config);
  assert.equal(fileMapping.kind, "file");
  assert.ok(fileMapping.filePath.endsWith("/app/bootstrap.js"));

  const htmlMapping = mapLocalAssetPath("/_debundle/live/example/live-index.html", config);
  assert.equal(htmlMapping.kind, "live-index");
  assert.match(htmlMapping.body.toString("utf8"), /js-debundle-live-proxy/);

  const swMapping = mapLocalAssetPath("/_debundle/live/example/sw.js", config);
  assert.equal(swMapping.kind, "service-worker");
  assert.match(swMapping.body.toString("utf8"), /skipWaiting/);
});

test("mapLocalAssetPath serves swapped vendor chunks from package roots and generated wrappers", () => {
  const fixture = writeBaseLiveProxyFixture("debundle-live-proxy-vendor-", { uiVersion: "vendor" });
  const vendorManifestPath = join(fixture.vendorsRoot, "manifest.json");

  writeTextFile(
    join(fixture.packagesRoot, "katex", "dist", "katex.mjs"),
    'export { helper } from "./helpers/helper.mjs";\nexport const render = () => "katex";\n'
  );
  writeTextFile(join(fixture.packagesRoot, "katex", "dist", "helpers", "helper.mjs"), "export const helper = 1;\n");
  writeJsonFile(join(fixture.packagesRoot, "katex", "package.json"), {
    name: "katex",
    version: "0.16.19",
  });
  writeTextFile(
    join(fixture.vendorsRoot, "generated", "static", "native-B5Vb9Oiz", "runtime.js"),
    "const data = { native: true };\nexport default data;\nexport const native = data.native;\n"
  );
  writeJsonFile(vendorManifestPath, {
    kind: "js.vendor_resolution_manifest",
    uiVersion: "vendor",
    resolutions: {
      "static/katex-BZy9Y_85.js": {
        chunkId: "static/katex-BZy9Y_85",
        chunkPath: "static/katex-BZy9Y_85.js",
        entryFile: "runtime.js",
        package: "katex",
        version: "0.16.19",
        subpath: "dist/katex.mjs",
      },
      "static/native-B5Vb9Oiz.js": {
        chunkId: "static/native-B5Vb9Oiz",
        chunkPath: "static/native-B5Vb9Oiz.js",
        entryFile: "runtime.js",
        package: "@emoji-mart/data",
        version: "1.2.1",
        subpath: "sets/15/native.json",
        wrapperShape: "named-from-json-default",
        // Vendor manifest sits at <root>/vendors/manifest.json; the wrapper
        // file is its sibling under generated/. With manifest-relative
        // resolution the recorded path is rooted at the vendor manifest's
        // own directory.
        generatedWrapperPath: "generated/static/native-B5Vb9Oiz/runtime.js",
      },
    },
  });
  writeJsonFile(fixture.appManifestPath, {
    assetSummaryPath: fixture.assetSummaryPath,
    outDir: fixture.appRoot,
    sourceHtml: fixture.sourceHtmlPath,
    uiVersion: "vendor",
    vendorManifestPath,
  });

  const config = loadLiveProxyConfiguration({
    appManifestPath: fixture.appManifestPath,
    assetHost: "127.0.0.1",
    assetPort: 9900,
    packagesRoot: fixture.packagesRoot,
    proxyHost: "127.0.0.1",
    proxyPort: 9800,
    stateDir: join(fixture.root, "state"),
  });

  const runtimeHit = mapLocalAssetPath("/_debundle/live/vendor/app/static/katex-BZy9Y_85/runtime.js", config);
  assert.equal(runtimeHit.kind, "vendor-file");
  assert.ok(runtimeHit.filePath.endsWith("/node_modules/katex/dist/katex.mjs"));
  assert.equal(runtimeHit.contentType, "text/javascript; charset=utf-8");

  const siblingHit = mapLocalAssetPath("/_debundle/live/vendor/app/static/katex-BZy9Y_85/helpers/helper.mjs", config);
  assert.equal(siblingHit.kind, "vendor-file");
  assert.ok(siblingHit.filePath.endsWith("/node_modules/katex/dist/helpers/helper.mjs"));

  const wrapperHit = mapLocalAssetPath("/_debundle/live/vendor/app/static/native-B5Vb9Oiz/runtime.js", config);
  assert.equal(wrapperHit.kind, "vendor-file");
  assert.equal(wrapperHit.filePath, join(fixture.vendorsRoot, "generated", "static", "native-B5Vb9Oiz", "runtime.js"));

  const appHit = mapLocalAssetPath("/_debundle/live/vendor/app/static/index-Example/runtime.js", config);
  assert.equal(appHit.kind, "file");
  assert.ok(appHit.filePath.endsWith("/app/static/index-Example/runtime.js"));
});

test("loadLiveProxyConfiguration tolerates missing vendor manifest and rejects path escapes", () => {
  const noVendorFixture = writeBaseLiveProxyFixture("debundle-live-proxy-novendor-", { uiVersion: "novendor" });
  assert.doesNotThrow(() =>
    loadLiveProxyConfiguration({
      appManifestPath: noVendorFixture.appManifestPath,
      assetHost: "127.0.0.1",
      assetPort: 9901,
      proxyHost: "127.0.0.1",
      proxyPort: 9801,
      stateDir: join(noVendorFixture.root, "state"),
    })
  );

  const escapeFixture = writeBaseLiveProxyFixture("debundle-live-proxy-escape-", { uiVersion: "escape" });
  assert.throws(
    () =>
      mapLocalAssetPath("/_debundle/live/escape/app/../../etc/passwd", {
        ...loadLiveProxyConfiguration({
          appManifestPath: escapeFixture.appManifestPath,
          assetHost: "127.0.0.1",
          assetPort: 9902,
          proxyHost: "127.0.0.1",
          proxyPort: 9802,
          stateDir: join(escapeFixture.root, "state"),
        }),
      }),
    /Refusing to serve path outside root/
  );
});
