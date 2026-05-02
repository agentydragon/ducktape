#!/usr/bin/env node
// Prepares the inputs the debundler expects from the smoke bundle:
//   <snapshot_out>/index.html    — copy of the smoke index.html, with
//                                  the <script type="module" src="/dist/main.js">
//                                  rewritten to the smoke bundle's main entry.
//   <snapshot_out>/dist/*.js      — chunks, copied verbatim from the smoke bundle.
//   <asset_summary_out>            — minimal asset-summary.json the
//                                    `emit_browser_harness` stage reads
//                                    (entryPoints.html + counts).
//   <js_list_out>                  — newline-delimited list of chunk paths,
//                                    relative to <snapshot_out>.
//
// The shape mirrors gaffer's tana/upstream/web/{snapshots,extracted}/<version>/.

import { copyFile, mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";

function parseArgs(argv) {
  const options = {};
  const expected = new Set(["--bundle", "--index-html", "--snapshot-out", "--asset-summary-out", "--js-list-out"]);
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index];
    if (!expected.has(arg)) {
      throw new Error(`Unknown argument: ${arg}`);
    }
    const next = argv[++index];
    if (next === undefined) {
      throw new Error(`${arg} requires a value`);
    }
    const key = arg.replace(/^--/, "").replaceAll("-", "_");
    options[key] = next;
  }
  for (const required of ["bundle", "index_html", "snapshot_out", "asset_summary_out", "js_list_out"]) {
    if (!options[required]) {
      throw new Error(`--${required.replaceAll("_", "-")} is required`);
    }
  }
  return options;
}

async function listChunks(bundleDir) {
  const entries = await readdir(bundleDir, { withFileTypes: true });
  return entries
    .filter((entry) => entry.isFile() && entry.name.endsWith(".js"))
    .map((entry) => entry.name)
    .sort();
}

function rewriteIndexHtml(html, mainHref) {
  // Replace the smoke `/dist/main.js` reference (and `/dist/main.css`)
  // with paths relative to the snapshot root. Use a strict, single-purpose
  // substring swap rather than HTML parsing — the smoke index.html is
  // hand-written and stable; if its layout drifts the test should fail
  // loudly rather than silently regex past the change.
  const cssOriginal = `<link rel="stylesheet" href="/dist/main.css" />`;
  const jsOriginal = `<script type="module" src="/dist/main.js"></script>`;
  const cssReplacement = `<link rel="stylesheet" href="./dist/main.css" />`;
  const jsReplacement = `<script type="module" src="./${mainHref}"></script>`;
  if (!html.includes(cssOriginal) || !html.includes(jsOriginal)) {
    throw new Error(
      `smoke_index.html shape changed; expected both ${JSON.stringify(cssOriginal)} and ${JSON.stringify(jsOriginal)} substrings to be present`
    );
  }
  return html.replace(cssOriginal, cssReplacement).replace(jsOriginal, jsReplacement);
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  // The js_binary launcher chdir's to `BAZEL_BINDIR` (the root of
  // the bazel-out tree) before invoking node. Path args from
  // `$(execpath …)` arrive execroot-relative, so we strip the
  // `BAZEL_BINDIR/` prefix when present to get a path relative to
  // the script's cwd.
  const bindir = process.env.BAZEL_BINDIR;
  const stripBindir = (path) => {
    if (!bindir) return path;
    const prefix = `${bindir}/`;
    return path.startsWith(prefix) ? path.slice(prefix.length) : path;
  };
  const bundleDir = stripBindir(options.bundle);
  const indexHtmlPath = stripBindir(options.index_html);
  const snapshotOut = stripBindir(options.snapshot_out);
  const assetSummaryOut = stripBindir(options.asset_summary_out);
  const jsListOut = stripBindir(options.js_list_out);

  await mkdir(snapshotOut, { recursive: true });
  await mkdir(join(snapshotOut, "dist"), { recursive: true });
  await mkdir(dirname(assetSummaryOut), { recursive: true });
  await mkdir(dirname(jsListOut), { recursive: true });

  const chunkNames = await listChunks(bundleDir);
  if (chunkNames.length === 0) {
    throw new Error(`No .js chunks found in ${bundleDir}`);
  }
  for (const chunkName of chunkNames) {
    await copyFile(join(bundleDir, chunkName), join(snapshotOut, "dist", chunkName));
  }
  // The CSS sits next to the chunks; copy it so the harness can serve it
  // alongside the JS once index.html references it.
  for (const auxName of ["main.css"]) {
    try {
      await copyFile(join(bundleDir, auxName), join(snapshotOut, "dist", auxName));
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }

  const mainChunk = chunkNames.find((name) => name === "main.js");
  if (!mainChunk) {
    throw new Error(`Smoke bundle is missing main.js (chunks: ${chunkNames.join(", ")})`);
  }
  const indexHtml = rewriteIndexHtml(await readFile(indexHtmlPath, "utf8"), `dist/${mainChunk}`);
  await writeFile(join(snapshotOut, "index.html"), indexHtml);

  const jsRelPaths = chunkNames.map((name) => `dist/${name}`);
  await writeFile(jsListOut, `${jsRelPaths.join("\n")}\n`);

  const assetSummary = {
    counts: {
      htmlFiles: 1,
      jsChunks: chunkNames.length,
      jsFiles: chunkNames.length,
    },
    entryPoints: {
      html: "index.html",
      js: [`dist/${mainChunk}`],
    },
  };
  await writeFile(assetSummaryOut, `${JSON.stringify(assetSummary, null, 2)}\n`);
}

await main();
