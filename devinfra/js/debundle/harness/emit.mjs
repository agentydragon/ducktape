import { chmodSync, copyFileSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, posix, relative, resolve, sep } from "node:path";
import { modulePackageJson, writeJsonFile, writeTextFile } from "../common/parser_options.mjs";
import { getArtifactManifestChunks, getChunkEntryPath, listChunkFilePaths, requireChunkFile, requirePipelineArtifact } from "../common/artifact.mjs";
import { prepareOutputDir, relativeWorkspacePath, resolveWorkspacePath } from "../common/io.mjs";
import { loadVendorResolutionManifest } from "../common/vendor_runtime.mjs";
import { serializeGeneratedJsFile } from "../split/chunk.mjs";

const MODULE_SCRIPT_RE =
  /<script\b(?=[^>]*\btype\s*=\s*["']module["'])(?=[^>]*\bsrc\s*=\s*["'][^"']+["'])[^>]*>\s*<\/script>/gi;
const MODULE_PRELOAD_RE =
  /<link\b(?=[^>]*\brel\s*=\s*["'][^"']*\bmodulepreload\b[^"']*["'])(?=[^>]*\bhref\s*=\s*["'][^"']+["'])[^>]*>/gi;


export function emitBrowserHarness(options) {
  const artifact = requirePipelineArtifact(options.artifact, "emitBrowserHarness");
  const snapshotRoot = resolveWorkspacePath(options.snapshotRoot);
  const assetSummaryPath = resolveWorkspacePath(options.assetSummaryPath);
  const scriptSource = options.scriptSource ?? "snapshot";
  if (scriptSource !== "split") {
    throw new Error(`Unsupported scriptSource: ${scriptSource}`);
  }
  const outDir = resolveWorkspacePath(options.outDir);

  const assetSummary = JSON.parse(readFileSync(assetSummaryPath, "utf8"));
  const snapshotManifest = {
    chunks: getArtifactManifestChunks(artifact),
  };
  const sourceHtmlPath = join(snapshotRoot, normalizeSnapshotPath(assetSummary.entryPoints?.html ?? "index.html"));
  const sourceHtml = readFileSync(sourceHtmlPath, "utf8");
  const scriptEntries = htmlScriptEntries(sourceHtml);
  const preloadEntries = htmlModulePreloadEntries(sourceHtml).filter((entry) => entry.path.endsWith(".js"));

  const entryScripts = scriptEntries
    .map((entry) => entry.path)
    .filter((path) => path.endsWith(".js"))
    .filter((path, index, paths) => paths.indexOf(path) === index);
  if (entryScripts.length === 0) {
    throw new Error(`No module script entry found in ${sourceHtmlPath}`);
  }

  const runtimeChunkIds = new Set(snapshotManifest.chunks.map((chunk) => chunk.chunkId));
  for (const path of [...entryScripts, ...preloadEntries.map((entry) => entry.path)]) {
    const chunkId = chunkIdForJsPath(path);
    if (!runtimeChunkIds.has(chunkId)) {
      throw new Error(`Snapshot manifest does not contain chunk ${chunkId}`);
    }
  }

  prepareOutputDir(outDir, { force: options.force });
  const vendorManifestPath = options.vendorManifestPath ? resolveWorkspacePath(options.vendorManifestPath) : undefined;
  const vendorResolutions = describeVendorResolutions(vendorManifestPath);
  materializeArtifactScripts({ artifact, outDir });
  const copiedAssets = copySnapshotAssets(snapshotRoot, outDir, { includeJavaScript: false });

  const bootstrapPath = join(outDir, "bootstrap.js");
  const indexPath = join(outDir, "index.html");
  const transformedManifestPath = join(outDir, "transformed-manifest.json");
  const bootstrap = buildBootstrap({ artifact, entryScripts, outDir, runtimeRoot: outDir, scriptSource });
  const indexHtml = rewriteIndexHtml(sourceHtml, {
    artifact,
    outDir,
    runtimeRoot: outDir,
    scriptSource,
  });
  const manifest = {
    schemaVersion: 1,
    scriptSource,
    sourceHtml: relativeWorkspacePath(sourceHtmlPath),
    snapshotRoot: relativeWorkspacePath(snapshotRoot),
    assetSummaryPath: relativeWorkspacePath(assetSummaryPath),
    runtimeManifestPath: relativeWorkspacePath(transformedManifestPath),
    runtimeRoot: relativeWorkspacePath(outDir),
    outDir: relativeWorkspacePath(outDir),
    copiedAssets,
    entryScripts,
    modulePreloads: preloadEntries.map((entry) => entry.path),
    vendorManifestPath: vendorManifestPath ? relativeWorkspacePath(vendorManifestPath) : null,
    vendorResolutions: vendorResolutions.map((entry) => ({
      ...entry,
      ...(entry.generatedWrapperPath
        ? { generatedWrapperPath: relativeWorkspacePath(entry.generatedWrapperPath) }
        : {}),
    })),
    generated: {
      bootstrap: relativeWorkspacePath(bootstrapPath),
      indexHtml: relativeWorkspacePath(indexPath),
      transformedManifest: relativeWorkspacePath(transformedManifestPath),
    },
  };

  writeFileSync(indexPath, indexHtml);
  writeFileSync(bootstrapPath, bootstrap);
  writeJsonFile(transformedManifestPath, snapshotManifest);
  writeJsonFile(join(outDir, "manifest.json"), manifest);
  writeJsonFile(join(outDir, "package.json"), modulePackageJson());
  return {
    artifact,
    manifest,
  };
}

function htmlScriptEntries(html) {
  return [...html.matchAll(MODULE_SCRIPT_RE)].map((match) => {
    const src = getAttr(match[0], "src");
    return {
      path: normalizeUrlPath(src),
      tag: match[0],
      url: src,
    };
  });
}

function htmlModulePreloadEntries(html) {
  return [...html.matchAll(MODULE_PRELOAD_RE)].map((match) => {
    const href = getAttr(match[0], "href");
    return {
      path: normalizeUrlPath(href),
      tag: match[0],
      url: href,
    };
  });
}

function rewriteIndexHtml(sourceHtml, { artifact, outDir, runtimeRoot, scriptSource }) {
  let scriptInserted = false;
  let html = sourceHtml.replace(MODULE_SCRIPT_RE, () => {
    if (scriptInserted) {
      return "";
    }
    scriptInserted = true;
    return `${harnessMonitorScript()}\n    <script type="module" src="./bootstrap.js"></script>`;
  });

  html = html.replace(MODULE_PRELOAD_RE, (tag) => {
    const href = getAttr(tag, "href");
    const path = normalizeUrlPath(href);
    if (!path.endsWith(".js")) {
      return rewriteRootAbsoluteUrls(tag);
    }
    return setAttr(tag, "href", scriptHref(path, { artifact, outDir, runtimeRoot, scriptSource }));
  });

  html = rewriteRootAbsoluteUrls(html);
  if (!scriptInserted) {
    html = html.replace(
      /<\/body>/i,
      `    ${harnessMonitorScript()}\n    <script type="module" src="./bootstrap.js"></script>\n  </body>`
    );
  }

  const comment =
    scriptSource === "split"
      ? "Generated local harness: loads generated runtime JavaScript from the transformed output tree."
      : "Generated local harness: loads copied prettified JavaScript from this directory.";
  if (!html.includes(comment)) {
    html = html.replace(/<head>/i, `<head>\n    <!-- ${comment} -->`);
  }
  return html.endsWith("\n") ? html : `${html}\n`;
}

function harnessMonitorScript() {
  return `<script>
      globalThis.__debundleHarness = { errors: [] };
      (() => {
        const state = globalThis.__debundleHarness;
        const render = (message) => {
          const body = document.body;
          if (!body) {
            return;
          }
          let node = document.getElementById("debundle-harness-error");
          if (!node) {
            node = document.createElement("pre");
            node.id = "debundle-harness-error";
            node.style.cssText = "position:fixed;inset:0;z-index:2147483647;margin:0;padding:16px;white-space:pre-wrap;background:#2b0000;color:#ffd8d8;font:13px/1.4 monospace;";
            body.appendChild(node);
          }
          node.textContent = message;
        };
        const messageFor = (kind, value) => {
          if (value && value.stack) {
            return value.stack;
          }
          if (value && typeof value === "object") {
            try {
              return JSON.stringify(value);
            } catch {
              return String(value);
            }
          }
          return String(value ?? kind);
        };
        const record = (kind, value, visible) => {
          const message = messageFor(kind, value);
          state.errors.push({ kind, message });
          document.documentElement.dataset.debundleHarnessLastEvent = message;
          if (kind === "error") {
            document.documentElement.dataset.debundleHarnessError = message;
          }
          if (visible) {
            if (document.readyState === "loading") {
              addEventListener("DOMContentLoaded", () => render(message), { once: true });
            } else {
              render(message);
            }
          }
        };
        addEventListener("error", (event) => record("error", event.error ?? event.message, true));
        addEventListener("unhandledrejection", (event) => record("unhandledrejection", event.reason, false));
        addEventListener("DOMContentLoaded", () => {
          document.documentElement.dataset.debundleHarnessDomContentLoaded = "true";
        });
        addEventListener("load", () => {
          document.documentElement.dataset.debundleHarnessLoaded = "true";
        });
      })();
    </script>`;
}

function rewriteRootAbsoluteUrls(html) {
  return html.replace(/\b(href|src)\s*=\s*(["'])\/(?!\/)([^"']*)\2/gi, (_match, attr, quote, path) => {
    return `${attr}=${quote}./${escapeHtmlAttr(path)}${quote}`;
  });
}

function buildBootstrap({ artifact, entryScripts, outDir, runtimeRoot, scriptSource }) {
  const lines = [
    "// Generated by //devinfra/js/debundle/transforms:run_transform.",
    `// Loads original HTML module script entries from ${scriptSource} output.`,
    "",
  ];

  for (const entryScript of entryScripts) {
    lines.push(`import ${JSON.stringify(scriptHref(entryScript, { artifact, outDir, runtimeRoot, scriptSource }))};`);
  }

  lines.push("");
  return lines.join("\n");
}

function materializeArtifactScripts({ artifact, outDir }) {
  for (const chunk of getArtifactManifestChunks(artifact)) {
    const chunkOutDir = join(outDir, ...chunk.chunkId.split("/"));
    for (const file of listChunkFilePaths(artifact, chunk.chunkId)) {
      const fileArtifact = requireChunkFile(artifact, chunk.chunkId, file, "emitBrowserHarness");
      const targetPath = join(chunkOutDir, ...file.split("/"));
      writeTextFile(targetPath, serializeGeneratedJsFile(fileArtifact));
    }
  }
}

function scriptHref(jsPath, { artifact, outDir, runtimeRoot, scriptSource }) {
  if (scriptSource === "snapshot") {
    return `./${normalizeSnapshotPath(jsPath)}`;
  }
  if (!runtimeRoot) {
    throw new Error("runtimeRoot is required for split script output");
  }
  return runtimeJsHref(artifact, jsPath, outDir, runtimeRoot);
}

function runtimeJsHref(artifact, jsPath, outDir, runtimeRoot) {
  const chunkId = chunkIdForJsPath(jsPath);
  const entryFile = getChunkEntryPath(artifact, chunkId);
  if (!entryFile) {
    throw new Error(`Missing chunk entry file for ${chunkId}`);
  }
  const entryPath = join(runtimeRoot, ...chunkId.split("/"), ...entryFile.split("/"));
  return relativeModuleSpecifier(outDir, entryPath);
}

function describeVendorResolutions(vendorManifestPath) {
  return Object.entries(loadVendorResolutionManifest(vendorManifestPath)).map(([resolutionChunkPath, entry]) => {
    const chunkPath = entry.chunkPath ?? resolutionChunkPath;
    if (typeof entry.entryFile !== "string" || entry.entryFile === "") {
      throw new Error(`Vendor resolution for ${chunkPath} is missing entryFile in ${vendorManifestPath}`);
    }
    return {
      chunkId: chunkIdForChunkPath(chunkPath),
      chunkPath,
      entryFile: entry.entryFile,
      package: entry.package,
      subpath: entry.subpath,
      version: entry.version,
      ...(entry.generatedWrapperPath
        ? { generatedWrapperPath: resolve(dirname(dirname(vendorManifestPath)), entry.generatedWrapperPath) }
        : {}),
    };
  });
}

function relativeModuleSpecifier(fromDir, targetPath) {
  let specifier = relative(fromDir, targetPath).split(sep).join("/");
  if (!specifier.startsWith(".")) {
    specifier = `./${specifier}`;
  }
  return specifier;
}

function copySnapshotAssets(snapshotRoot, outDir, { includeJavaScript }) {
  const copied = [];
  copyRecursive("");
  return copied;

  function copyRecursive(relativeDir) {
    const absoluteDir = join(snapshotRoot, relativeDir);
    for (const entry of readdirSync(absoluteDir, { withFileTypes: true })) {
      const relativeEntryPath = relativeDir === "" ? entry.name : posix.join(relativeDir, entry.name);
      const absoluteEntryPath = join(snapshotRoot, ...relativeEntryPath.split("/"));
      if (entry.isDirectory()) {
        copyRecursive(relativeEntryPath);
        continue;
      }
      if (!entry.isFile() || shouldSkipSnapshotAsset(relativeEntryPath, { includeJavaScript })) {
        continue;
      }
      const outPath = join(outDir, ...relativeEntryPath.split("/"));
      copyFileWithParents(absoluteEntryPath, outPath);
      copied.push(relativeEntryPath);
    }
  }
}

function copyFileWithParents(sourcePath, outputPath) {
  writeTextFile(outputPath, "");
  copyFileSync(sourcePath, outputPath);
  chmodSync(outputPath, 0o644);
}

function shouldSkipSnapshotAsset(relativePath, { includeJavaScript }) {
  if (includeJavaScript) {
    return false;
  }
  return relativePath.endsWith(".js") || relativePath.endsWith(".js.map");
}

function normalizeUrlPath(url) {
  if (!url) {
    throw new Error("Expected a non-empty URL path");
  }
  if (/^[A-Za-z][A-Za-z0-9+.-]*:/.test(url) || url.startsWith("//")) {
    throw new Error(`Expected a snapshot-relative URL, got ${url}`);
  }
  const withoutHash = url.split("#", 1)[0];
  const withoutQuery = withoutHash.split("?", 1)[0];
  const stripped = withoutQuery.startsWith("/") ? withoutQuery.slice(1) : withoutQuery.replace(/^\.\//, "");
  return normalizeSnapshotPath(stripped);
}

function normalizeSnapshotPath(path) {
  const normalized = posix.normalize(path.split("\\").join("/"));
  if (normalized === "" || normalized === "." || normalized.startsWith("/") || normalized.split("/").includes("..")) {
    throw new Error(`Invalid snapshot-relative path: ${path}`);
  }
  return normalized;
}

function chunkIdForJsPath(jsPath) {
  const path = normalizeSnapshotPath(jsPath);
  if (!path.endsWith(".js")) {
    throw new Error(`Expected a .js path: ${jsPath}`);
  }
  return path.slice(0, -".js".length);
}

function chunkIdForChunkPath(chunkPath) {
  if (!chunkPath.endsWith(".js")) {
    throw new Error(`Expected a .js chunkPath: ${chunkPath}`);
  }
  return chunkPath.slice(0, -".js".length);
}

function getAttr(tag, name) {
  const match = tag.match(new RegExp(`\\b${name}\\s*=\\s*("([^"]*)"|'([^']*)'|([^\\s>]+))`, "i"));
  if (!match) {
    throw new Error(`Tag is missing ${name}: ${tag}`);
  }
  return match[2] ?? match[3] ?? match[4] ?? "";
}

function setAttr(tag, name, value) {
  const escaped = escapeHtmlAttr(value);
  const attrRe = new RegExp(`\\b${name}\\s*=\\s*("([^"]*)"|'([^']*)'|([^\\s>]+))`, "i");
  if (attrRe.test(tag)) {
    return tag.replace(attrRe, (_match, raw, doubleQuoted, singleQuoted) => {
      if (singleQuoted !== undefined) {
        return `${name}='${escaped}'`;
      }
      if (doubleQuoted !== undefined) {
        return `${name}="${escaped}"`;
      }
      return `${name}="${escaped}"`;
    });
  }
  return tag.replace(/\s*\/?>$/, ` ${name}="${escaped}"$&`);
}

function escapeHtmlAttr(value) {
  return value.replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;");
}
