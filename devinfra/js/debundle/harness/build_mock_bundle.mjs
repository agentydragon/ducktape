import { copyFile, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "vite";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SOURCE_ROOT = resolve(__dirname, "testdata/mock_browser_bundle/source");
const ENTRY_OUTPUT = "static/index-DuckMock.js";

export async function buildMockBrowserBundle(outRoot) {
  const root = resolve(outRoot);
  const extractedRoot = join(root, "extracted");
  const snapshotRoot = join(root, "snapshot");
  const staticRoot = join(snapshotRoot, "static");

  await rm(root, { force: true, recursive: true });
  await mkdir(join(snapshotRoot, "preload"), { recursive: true });
  await mkdir(staticRoot, { recursive: true });
  await copyFile(join(SOURCE_ROOT, "preload", "app.css"), join(snapshotRoot, "preload", "app.css"));

  await build({
    appType: "custom",
    configFile: false,
    publicDir: false,
    resolve: {
      preserveSymlinks: false,
    },
    build: {
      emptyOutDir: false,
      manifest: false,
      minify: false,
      modulePreload: false,
      outDir: staticRoot,
      rollupOptions: {
        input: {
          index: join(SOURCE_ROOT, "src", "index.js"),
        },
        output: {
          chunkFileNames: "[name]-DuckMock.js",
          entryFileNames: "[name]-DuckMock.js",
          format: "es",
          manualChunks(id) {
            if (id.endsWith("/SharedFormat.js")) {
              return "chunk";
            }
            return undefined;
          },
        },
      },
      sourcemap: false,
      target: "es2022",
    },
  });

  const jsFiles = await listStaticJsFiles(staticRoot);
  await writeHtml(snapshotRoot, jsFiles);
  await writeExtractedMetadata(jsFiles, extractedRoot);
}

async function writeHtml(snapshotRoot, jsFiles) {
  const template = await readFile(join(SOURCE_ROOT, "index.html"), "utf8");
  const preloads = jsFiles
    .filter((path) => path !== ENTRY_OUTPUT)
    .map((path) => `    <link rel="modulepreload" crossorigin href="/${path}" />`)
    .join("\n");
  const rendered = template
    .replaceAll("__ENTRY_SCRIPT__", `/${ENTRY_OUTPUT}`)
    .replace("__MODULE_PRELOADS__", preloads === "" ? "" : `${preloads}\n`);
  await writeFile(join(snapshotRoot, "index.html"), rendered);
}

async function writeExtractedMetadata(jsFiles, extractedRoot) {
  const assetSummary = {
    uiVersion: "fixture",
    entryPoints: {
      css: ["preload/app.css"],
      html: "index.html",
      js: jsFiles,
    },
  };

  await mkdir(extractedRoot, { recursive: true });
  await writeFile(join(extractedRoot, "asset-summary.json"), `${JSON.stringify(assetSummary, null, 2)}\n`);
  await writeFile(join(extractedRoot, "js-files.txt"), `${jsFiles.join("\n")}\n`);
}

async function listStaticJsFiles(staticRoot) {
  return (await readdir(staticRoot))
    .filter((name) => name.endsWith(".js"))
    .map((name) => `static/${name}`)
    .sort();
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const outRoot = process.argv[2];
  if (!outRoot) {
    throw new Error("usage: build_mock_bundle.mjs <out-root>");
  }
  await buildMockBrowserBundle(outRoot);
}
