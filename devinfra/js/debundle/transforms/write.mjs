import { join } from "node:path";
import { modulePackageJson, writeJsonFile, writeTextFile } from "../common/parser_options.mjs";
import {
  getArtifactChunkManifest,
  getArtifactManifest,
  listChunks,
  requirePipelineArtifact,
} from "../common/artifact.mjs";
import { prepareOutputDir, relativeWorkspacePath, resolveWorkspacePath } from "../common/io.mjs";
import { serializeGeneratedJsFile } from "../split/chunk.mjs";

export function writeJsTree({ artifact, force = false, outDir }) {
  requirePipelineArtifact(artifact, "writeJsTree");
  if (typeof outDir !== "string" || outDir === "") {
    throw new Error("writeJsTree requires outDir");
  }
  const resolvedOutDir = resolveWorkspacePath(outDir);

  prepareOutputDir(resolvedOutDir, { force });

  const chunkEntries = listChunks(artifact);
  const files = [];
  for (const { chunkId, files: chunkFiles } of chunkEntries) {
    for (const file of chunkFiles) {
      const outputPath = join(resolvedOutDir, ...chunkId.split("/"), ...file.path.split("/"));
      writeTextFile(outputPath, serializeGeneratedJsFile(file));
      files.push(`${chunkId}/${file.path}`);
    }
  }

  const snapshotManifest = getArtifactManifest(artifact);
  if (snapshotManifest) {
    writeJsonFile(join(resolvedOutDir, "manifest.json"), snapshotManifest);
  }
  for (const { chunkId } of chunkEntries) {
    const chunkManifest = getArtifactChunkManifest(artifact, chunkId);
    if (chunkManifest) {
      writeJsonFile(join(resolvedOutDir, ...chunkId.split("/"), "manifest.json"), chunkManifest);
    }
  }
  writeJsonFile(join(resolvedOutDir, "package.json"), modulePackageJson());

  return {
    artifact,
    manifest: {
      kind: "js.write_js_tree_manifest",
      outDir: relativeWorkspacePath(resolvedOutDir),
      counts: {
        chunks: chunkEntries.length,
        files: files.length,
      },
      files,
    },
  };
}
