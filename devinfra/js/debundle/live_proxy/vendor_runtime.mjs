import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve } from "node:path";
import { posix } from "node:path";
import { assertRealPathWithinRoot, resolvePackageSubpath } from "./package_tree.mjs";

export function loadVendorResolutionManifest(manifestPath) {
  if (!manifestPath || !existsSync(manifestPath)) {
    return {};
  }
  const raw = JSON.parse(readFileSync(manifestPath, "utf8"));
  if (raw.kind !== "js.vendor_resolution_manifest") {
    throw new Error(`Unexpected vendor manifest kind: ${raw.kind} at ${manifestPath}`);
  }
  return raw.resolutions ?? {};
}

export function loadVendorRuntimeIndex({ manifestPath, packageRoots, packagesRoot }) {
  const resolutions = loadVendorResolutionManifest(manifestPath);
  const byChunkId = new Map();
  const manifestDir = dirname(manifestPath);
  for (const [resolutionChunkPath, entry] of Object.entries(resolutions)) {
    const chunkPath = entry.chunkPath ?? resolutionChunkPath;
    if (!entry.package || !entry.subpath || !entry.version) {
      throw new Error(`Vendor resolution for ${chunkPath} is missing package/version/subpath in ${manifestPath}`);
    }
    const chunkId = chunkIdForChunkPath(chunkPath);
    const entryFile = resolveVendorManifestEntryFile(entry, { chunkPath, manifestPath });
    const wrapperAbsPath = entry.generatedWrapperPath ? resolve(manifestDir, entry.generatedWrapperPath) : null;
    const filePath =
      wrapperAbsPath ?? resolvePackageSubpath(entry.package, entry.subpath, { packageRoots, packagesRoot });
    const mountRoot = resolveMountRoot(filePath, entryFile);
    const mountedEntryFile = normalizeMountedRelativePath(relative(mountRoot, filePath));
    byChunkId.set(chunkId, {
      chunkId,
      chunkPath,
      entryFile,
      mountedEntryFile,
      filePath,
      mountRoot,
      package: entry.package,
      subpath: entry.subpath,
      version: entry.version,
      ...(wrapperAbsPath
        ? {
            generatedWrapperPath: wrapperAbsPath,
            wrapperShape: entry.wrapperShape ?? "generated-wrapper",
          }
        : {}),
    });
  }
  return byChunkId;
}

export function resolveVendorRuntimeRequest(relativePath, vendorRuntimeIndex) {
  if (!vendorRuntimeIndex || vendorRuntimeIndex.size === 0) {
    return null;
  }
  const normalizedPath = normalizeRelativePath(relativePath);
  const candidatePaths = normalizedPath.startsWith("app/")
    ? [normalizedPath, normalizedPath.slice("app/".length)]
    : [normalizedPath];
  for (const candidatePath of candidatePaths) {
    for (const entry of vendorRuntimeIndex.values()) {
      const prefix = `${entry.chunkId}/`;
      if (!candidatePath.startsWith(prefix)) {
        continue;
      }
      const suffix = candidatePath.slice(prefix.length);
      return {
        ...entry,
        filePath: resolveVendorMountedPath(entry, suffix),
        requestPath: candidatePath,
        requestSuffix: suffix,
      };
    }
  }
  return null;
}

function chunkIdForChunkPath(chunkPath) {
  if (!chunkPath.endsWith(".js")) {
    throw new Error(`Expected .js chunk path, got ${chunkPath}`);
  }
  return chunkPath.slice(0, -".js".length);
}

function normalizeRelativePath(relativePath) {
  return `${relativePath ?? ""}`.split("?", 1)[0].replace(/^[/\\]+/, "");
}

function resolveVendorMountedPath(entry, suffix) {
  if (suffix === "" || suffix === ".") {
    throw new Error(`Invalid vendor request path for ${entry.chunkId}: ${suffix}`);
  }
  const mountedRelativePath = aliasVendorEntryPath(entry, suffix);
  const resolvedPath = resolve(entry.mountRoot, mountedRelativePath);
  assertPathWithinRoot(
    resolvedPath,
    entry.mountRoot,
    `Vendor request escapes mounted root for ${entry.chunkId}: ${mountedRelativePath}`
  );
  if (existsSync(resolvedPath)) {
    assertRealPathWithinRoot(
      resolvedPath,
      entry.mountRoot,
      `Vendor request realpath escapes mounted root for ${entry.chunkId}: ${mountedRelativePath}`
    );
  }
  return resolvedPath;
}

function aliasVendorEntryPath(entry, suffix) {
  if (suffix === entry.entryFile) {
    return entry.mountedEntryFile;
  }
  if (suffix === "runtime.js" && isRootMountedEntryFile(entry.entryFile)) {
    return entry.mountedEntryFile;
  }
  return suffix;
}

function normalizeEntryFile(entryFile) {
  const normalized = posix.normalize(`${entryFile ?? ""}`.replaceAll("\\", "/"));
  if (normalized === "" || normalized === "." || normalized.startsWith("/") || normalized.split("/").includes("..")) {
    throw new Error(`Invalid vendor entry file: ${entryFile}`);
  }
  return normalized;
}

function resolveVendorManifestEntryFile(entry, { chunkPath, manifestPath } = {}) {
  if (typeof entry?.entryFile !== "string" || entry.entryFile === "") {
    throw new Error(`Vendor resolution for ${chunkPath} is missing entryFile in ${manifestPath}`);
  }
  return normalizeEntryFile(entry.entryFile);
}

function normalizeMountedRelativePath(relativePath) {
  const normalized = posix.normalize(`${relativePath ?? ""}`.replaceAll("\\", "/"));
  if (normalized === "" || normalized === "." || normalized.startsWith("/") || normalized.split("/").includes("..")) {
    throw new Error(`Invalid mounted vendor path: ${relativePath}`);
  }
  return normalized;
}

function resolveMountRoot(filePath, entryFile) {
  const entryDir = posix.dirname(entryFile);
  if (entryDir === ".") {
    return dirname(filePath);
  }
  const depth = entryDir.split("/").length;
  return resolve(dirname(filePath), ...Array(depth).fill(".."));
}

function isRootMountedEntryFile(entryFile) {
  return !entryFile.includes("/");
}

function assertPathWithinRoot(path, root, message) {
  const rel = relative(root, path);
  if (rel === "" || rel === ".") {
    return;
  }
  if (rel.startsWith("..") || isAbsolute(rel)) {
    throw new Error(`${message}: ${path}`);
  }
}
