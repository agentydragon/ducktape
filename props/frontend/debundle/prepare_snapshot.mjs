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
//   <vendor_map_out>              — JSON: which output chunk holds each
//                                    swapped vendor package's code.
//
// The shape mirrors gaffer's tana/upstream/web/{snapshots,extracted}/<version>/.

import { copyFile, mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";

function parseArgs(argv) {
  const options = {};
  const expected = new Set([
    "--bundle",
    "--index-html",
    "--snapshot-out",
    "--asset-summary-out",
    "--js-list-out",
    "--vendor-map-out",
  ]);
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
  for (const required of [
    "bundle",
    "index_html",
    "snapshot_out",
    "asset_summary_out",
    "js_list_out",
    "vendor_map_out",
  ]) {
    if (!options[required]) {
      throw new Error(`--${required.replaceAll("_", "-")} is required`);
    }
  }
  return options;
}

async function listChunks(bundleDir) {
  // Bazel materializes tree-artifact entries as symlinks back to the
  // execroot, so `withFileTypes: true` returns DirEntries with
  // `isSymbolicLink() === true`. Filter on the .js extension and on
  // either real-file or symlink — anything other than a directory is
  // a chunk.
  const entries = await readdir(bundleDir, { withFileTypes: true });
  return entries
    .filter((entry) => !entry.isDirectory() && entry.name.endsWith(".js"))
    .map((entry) => entry.name)
    .sort();
}

function rewriteIndexHtml(html, mainHref) {
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

// Classify smoke-bundle chunks by reading the esbuild metafile. The
// smoke build emits one entry per vendor marker and the main app
// entry; esbuild's chunk-graph algorithm puts each vendor's code in
// a chunk shared by exactly that marker entry and main. So a chunk
// is identified as `<package>` iff it's imported by main and by
// `<package>_marker.js` (and no other entry).
function classifyChunks(metafile) {
  const outputs = metafile.outputs ?? {};
  const importsOf = new Map();
  for (const [name, info] of Object.entries(outputs)) {
    if (!name.endsWith(".js")) continue;
    importsOf.set(name, new Set((info.imports ?? []).map((entry) => entry.path)));
  }
  const findEntry = (suffix) => {
    for (const [name, info] of Object.entries(outputs)) {
      if (info.entryPoint && info.entryPoint.endsWith(suffix)) {
        return name;
      }
    }
    return null;
  };
  const mainEntry = findEntry("/smoke_main.ts");
  const highlightEntry = findEntry("/vendor_highlight_marker.ts");
  const datatableEntry = findEntry("/vendor_datatable_marker.ts");
  const allEntries = Object.entries(outputs)
    .filter(([_, info]) => info.entryPoint)
    .map(([name]) => name);
  const sharedExclusively = (entries) => {
    const requiredImporters = entries.filter((entry) => entry !== null);
    if (requiredImporters.length === 0) return [];
    const candidates = new Set(importsOf.get(requiredImporters[0]) ?? []);
    for (const entry of requiredImporters.slice(1)) {
      const importerSet = importsOf.get(entry) ?? new Set();
      for (const candidate of [...candidates]) {
        if (!importerSet.has(candidate)) {
          candidates.delete(candidate);
        }
      }
    }
    const otherEntries = allEntries.filter((name) => !requiredImporters.includes(name));
    for (const candidate of [...candidates]) {
      for (const otherEntry of otherEntries) {
        if (importsOf.get(otherEntry)?.has(candidate)) {
          candidates.delete(candidate);
          break;
        }
      }
    }
    return [...candidates];
  };
  const findVendorChunk = (markerEntry) => {
    if (!markerEntry || !mainEntry) return null;
    const candidates = sharedExclusively([mainEntry, markerEntry]);
    if (candidates.length === 0) return null;
    // Pick the largest chunk if esbuild splits the package across
    // multiple shared chunks (e.g. CJS shim + main payload).
    return candidates.sort((a, b) => (outputs[b]?.bytes ?? 0) - (outputs[a]?.bytes ?? 0))[0];
  };
  const baseName = (path) => path.split("/").pop();
  const highlightChunk = findVendorChunk(highlightEntry);
  const datatableChunk = findVendorChunk(datatableEntry);
  return {
    main: mainEntry ? baseName(mainEntry) : null,
    highlight: highlightChunk ? baseName(highlightChunk) : null,
    datatable: datatableChunk ? baseName(datatableChunk) : null,
  };
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
  const vendorMapOut = stripBindir(options.vendor_map_out);

  await mkdir(snapshotOut, { recursive: true });
  await mkdir(join(snapshotOut, "dist"), { recursive: true });
  await mkdir(dirname(assetSummaryOut), { recursive: true });
  await mkdir(dirname(jsListOut), { recursive: true });
  await mkdir(dirname(vendorMapOut), { recursive: true });

  const chunkNames = await listChunks(bundleDir);
  if (chunkNames.length === 0) {
    throw new Error(`No .js chunks found in ${bundleDir}`);
  }
  for (const chunkName of chunkNames) {
    await copyFile(join(bundleDir, chunkName), join(snapshotOut, "dist", chunkName));
  }
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

  // Classify chunks via the metafile so the spec generator's
  // vendor-mark ops can pin to the correct hashed chunk filenames.
  const metafilePath = join(bundleDir, "metafile.json");
  const metafile = JSON.parse(await readFile(metafilePath, "utf8"));
  const vendorMap = classifyChunks(metafile);
  if (!vendorMap.highlight) {
    throw new Error(
      `Could not classify highlight.js chunk in smoke bundle metafile (chunks: ${chunkNames.join(", ")})`
    );
  }
  if (!vendorMap.datatable) {
    throw new Error(
      `Could not classify @careswitch/svelte-data-table chunk in smoke bundle metafile (chunks: ${chunkNames.join(", ")})`
    );
  }
  const vendorMapPayload = {
    chunks: {
      "highlight.js": `dist/${vendorMap.highlight}`,
      "@careswitch/svelte-data-table": `dist/${vendorMap.datatable}`,
    },
  };
  await writeFile(vendorMapOut, `${JSON.stringify(vendorMapPayload, null, 2)}\n`);
}

await main();
