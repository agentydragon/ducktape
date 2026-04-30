import { readFileSync } from "node:fs";
import { basename, resolve } from "node:path";
import { createEmptyArtifact, createFile, createChunk, setChunk } from "./artifact.mjs";
import { resolveWorkspacePath } from "./io.mjs";
import { chunkIdForJsPath, normalizeAssetPath } from "./normalize.mjs";

export function loadJsChunks({ inputRoot, jsListPath }) {
  const resolvedInputRoot = resolveWorkspacePath(inputRoot);
  const resolvedJsListPath = resolveWorkspacePath(jsListPath);
  const jsFiles = parseJsList(readFileSync(resolvedJsListPath, "utf8"));
  const artifact = createEmptyArtifact();

  for (const sourcePath of jsFiles) {
    const absolutePath = resolve(resolvedInputRoot, ...sourcePath.split("/"));
    const chunkId = chunkIdForJsPath(sourcePath);
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
        chunkId: chunkIdForJsPath(sourcePath),
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
