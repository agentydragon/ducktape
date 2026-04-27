import { readFileSync } from "node:fs";
import { basename, posix, resolve } from "node:path";
import { createEmptyArtifact, createFile, createChunk, setChunk } from "./artifact.mjs";
import { resolveWorkspacePath } from "./io.mjs";


export function loadJsChunks({ inputRoot, jsListPath }) {
  const resolvedInputRoot = resolveWorkspacePath(inputRoot);
  const resolvedJsListPath = resolveWorkspacePath(jsListPath);
  const jsFiles = parseJsList(readFileSync(resolvedJsListPath, "utf8"));
  const artifact = createEmptyArtifact();

  for (const sourcePath of jsFiles) {
    const absolutePath = resolve(resolvedInputRoot, ...sourcePath.split("/"));
    const chunkId = sourcePath.slice(0, -".js".length);
    const entryFile = basename(sourcePath);
    setChunk(
      artifact,
      createChunk({
        chunkId,
        entryFile,
        files: [
          createFile({
            path: entryFile,
            content: readFileSync(absolutePath, "utf8"),
            metadata: {
              role: "entry",
              sourcePath,
            },
          }),
        ],
        metadata: {
          sourcePath,
        },
      })
    );
  }

  return {
    artifact,
    manifest: {
      kind: "js.loaded_js_chunks",
      counts: {
        chunks: jsFiles.length,
        files: jsFiles.length,
      },
      chunks: jsFiles.map((sourcePath) => ({
        chunkId: sourcePath.slice(0, -".js".length),
        entryFile: basename(sourcePath),
        sourcePath,
      })),
      jsFiles,
    },
  };
}

function parseJsList(text) {
  const paths = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line !== "" && !line.startsWith("#"))
    .map(normalizeAssetPath);
  const unique = new Set(paths);
  if (unique.size !== paths.length) {
    throw new Error("JS list contains duplicate paths");
  }
  return paths;
}

function normalizeAssetPath(path) {
  const normalized = path.split("\\").join("/");
  if (normalized === "" || normalized.startsWith("/") || normalized.split("/").includes("..")) {
    throw new Error(`Invalid input-root-relative JS path: ${path}`);
  }
  if (!normalized.endsWith(".js")) {
    throw new Error(`Expected a .js path in JS list: ${path}`);
  }
  return posix.normalize(normalized);
}
