import { existsSync, mkdirSync, readdirSync, rmSync, statSync } from "node:fs";
import { isAbsolute, relative, resolve, sep } from "node:path";

export function requireValue(argv, index, flag) {
  const value = argv[index];
  if (!value || value.startsWith("--")) {
    throw new Error(`${flag} requires a value`);
  }
  return value;
}

export function resolveWorkspacePath(path) {
  if (isAbsolute(path)) {
    return path;
  }
  const workspace =
    process.env.BUILD_WORKSPACE_DIRECTORY ?? process.env.BUILD_WORKING_DIRECTORY ?? process.env.PWD;
  if (workspace) {
    return resolve(workspace, path);
  }
  return resolve(process.cwd(), path);
}

export function prepareOutputDir(outDir, { force }) {
  if (existsSync(outDir)) {
    if (!statSync(outDir).isDirectory()) {
      throw new Error(`Output path exists and is not a directory: ${outDir}`);
    }
    const entries = readdirSync(outDir);
    if (entries.length > 0 && !force) {
      throw new Error(`Output directory is not empty: ${outDir}. Pass --force to replace it.`);
    }
    if (force) {
      rmSync(outDir, { force: true, recursive: true });
    }
  }
  mkdirSync(outDir, { recursive: true });
}

export function ensureOutputDir(outDir) {
  if (existsSync(outDir) && !statSync(outDir).isDirectory()) {
    throw new Error(`Output path exists and is not a directory: ${outDir}`);
  }
  mkdirSync(outDir, { recursive: true });
}

export function relativeWorkspacePath(path) {
  const workspace =
    process.env.BUILD_WORKSPACE_DIRECTORY ?? process.env.BUILD_WORKING_DIRECTORY ?? process.env.PWD;
  if (!workspace) {
    return path;
  }
  const rel = relative(workspace, path);
  if (rel === "" || rel.startsWith("..")) {
    return path;
  }
  return rel.split(sep).join("/");
}

export function logProgress(message) {
  process.stderr.write(`[${new Date().toISOString()}] ${message}\n`);
}

export function formatDuration(durationMs) {
  return `${(durationMs / 1000).toFixed(3)}s`;
}

export function formatDurationSince(startedAt) {
  const durationMs = Number(process.hrtime.bigint() - startedAt) / 1_000_000;
  return formatDuration(durationMs);
}
