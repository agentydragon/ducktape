import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve } from "node:path";
import { posix } from "node:path";
import { assertRealPathWithinRoot, resolvePackageSubpath } from "./package_tree.mjs";

export function loadVendorResolutionManifest(manifestPath) {
  if (!manifestPath || !existsSync(manifestPath)) {
    return {};
  }
  const raw = JSON.parse(readFileSync(manifestPath, "utf8"));
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error(`Vendor manifest must be a JSON object at ${manifestPath}`);
  }
  return raw.full ?? {};
}

export function loadVendorRuntimeIndex({ manifestPath, packageRoots, packagesRoot }) {
  const resolutions = loadVendorResolutionManifest(manifestPath);
  const byChunkId = new Map();
  const manifestDir = dirname(manifestPath);
  for (const [resolutionChunkPath, entry] of Object.entries(resolutions)) {
    const chunkPath = entry.chunk_path ?? resolutionChunkPath;
    if (!entry.package || !entry.subpath || !entry.version) {
      throw new Error(`Vendor resolution for ${chunkPath} is missing package/version/subpath in ${manifestPath}`);
    }
    const chunkId = chunkIdForChunkPath(chunkPath);
    const entryFile = resolveVendorManifestEntryFile(entry, { chunkPath, manifestPath });
    const wrapperAbsPath = entry.generated_wrapper_path ? resolve(manifestDir, entry.generated_wrapper_path) : null;
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
            wrapperShape: entry.wrapper_shape ?? "generated_wrapper",
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

// Path segment under <appAssetPrefix> that the live proxy mounts
// partial-swap packages under. Each partial-swap'd package gets a
// directory rooted at `<PARTIAL_SWAP_URL_PREFIX>/<package_name>/`
// where the package's filesystem root is served verbatim. Bare-specifier
// imports like `from "mobx-react-lite"` are resolved by an injected
// `<script type="importmap">` whose entries point at the package's
// `subpath` under this prefix.
const PARTIAL_SWAP_URL_PREFIX = "_partial_swap";

export function loadPartialSwapRuntimeIndex({ manifestPath, packageRoots, packagesRoot }) {
  const byPackage = new Map();
  if (!manifestPath || !existsSync(manifestPath)) {
    return byPackage;
  }
  const raw = JSON.parse(readFileSync(manifestPath, "utf8"));
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error(`Partial-swap manifest must be a JSON object at ${manifestPath}`);
  }
  const resolutions = raw.partial ?? {};
  for (const entry of Object.values(resolutions)) {
    for (const [packageName, packageEntry] of Object.entries(entry.packages ?? {})) {
      if (!packageEntry.subpath || !packageEntry.version) {
        throw new Error(`Partial-swap resolution for ${packageName} is missing subpath/version in ${manifestPath}`);
      }
      const filePath = resolvePackageSubpath(packageName, packageEntry.subpath, {
        packageRoots,
        packagesRoot,
      });
      const mountRoot = resolvePackageMountRoot(packageName, {
        packageRoots,
        packagesRoot,
        filePath,
      });
      byPackage.set(packageName, {
        package: packageName,
        version: packageEntry.version,
        subpath: packageEntry.subpath,
        filePath,
        mountRoot,
        mountedSubpath: normalizeMountedRelativePath(relative(mountRoot, filePath)),
        urlPrefix: `${PARTIAL_SWAP_URL_PREFIX}/${packageName}`,
      });
    }
  }
  return byPackage;
}

// Build the importmap object (suitable for JSON-stringifying into a
// `<script type="importmap">`) for a partial-swap runtime index. Each
// package's bare specifier (`"mobx-react-lite"`) maps to the URL where
// the live proxy serves the package's `subpath` file.
export function buildPartialSwapImportMap(partialSwapIndex, appAssetPrefix) {
  const imports = {};
  for (const entry of partialSwapIndex.values()) {
    imports[entry.package] = `${appAssetPrefix}/${entry.urlPrefix}/${entry.mountedSubpath}`;
  }
  return { imports };
}

// Resolve a request under the partial-swap URL prefix to an absolute
// filesystem path. Returns `null` when the path doesn't reference a
// partial-swap package; otherwise returns the file path, the package
// name, and the in-package relative path so the caller can serve the
// file with an accurate `Content-Type` and stay within the mount root.
export function resolvePartialSwapRuntimeRequest(relativePath, partialSwapIndex) {
  if (!partialSwapIndex || partialSwapIndex.size === 0) {
    return null;
  }
  const normalizedPath = normalizeRelativePath(relativePath);
  const candidatePaths = normalizedPath.startsWith("app/")
    ? [normalizedPath, normalizedPath.slice("app/".length)]
    : [normalizedPath];
  for (const candidatePath of candidatePaths) {
    const partialPrefix = `${PARTIAL_SWAP_URL_PREFIX}/`;
    if (!candidatePath.startsWith(partialPrefix)) {
      continue;
    }
    const rest = candidatePath.slice(partialPrefix.length);
    for (const entry of partialSwapIndex.values()) {
      const packagePrefix = `${entry.package}/`;
      if (!rest.startsWith(packagePrefix)) {
        continue;
      }
      const suffix = rest.slice(packagePrefix.length);
      const { filePath, resolvedSuffix } = resolvePartialSwapAssetPath(entry.mountRoot, suffix);
      assertPathWithinRoot(
        filePath,
        entry.mountRoot,
        `Partial-swap request escapes mounted root for ${entry.package}: ${suffix}`
      );
      if (existsSync(filePath)) {
        assertRealPathWithinRoot(
          filePath,
          entry.mountRoot,
          `Partial-swap request realpath escapes mounted root for ${entry.package}: ${suffix}`
        );
      }
      return {
        package: entry.package,
        version: entry.version,
        filePath,
        requestPath: candidatePath,
        requestSuffix: suffix,
        resolvedSuffix,
      };
    }
  }
  return null;
}

// Node-style auto-extension resolution for partial-swap package files.
// npm packages frequently emit ESM with extension-less internal imports
// (`import "./utils"` rather than `./utils.js`); the browser doesn't
// rewrite those, so the proxy tries `.js` / `.mjs` / `.cjs` / `/index.*`
// in order, mirroring Node's CommonJS-influenced resolver. Returns the
// first existing file path along with the in-package suffix that maps
// to it; otherwise returns the exact (file-less) suffix the caller
// requested so the proxy can surface a clean 404. A directory at the
// exact path is treated as not-yet-resolved — Node would fall through
// to its `index.*` lookup, and so do we.
//
// The returned `resolvedSuffix` may differ from the requested `suffix`
// (e.g. when `./platform` resolves to `platform/index.js`). The caller
// is expected to issue a 301 to the resolved URL so the browser's
// module base URL tracks the actual served file — otherwise relative
// imports inside that file (`./node` becoming `platform/node` vs
// `node`) would resolve from the wrong directory.
function resolvePartialSwapAssetPath(mountRoot, suffix) {
  const exact = resolve(mountRoot, suffix);
  if (isExistingFile(exact)) {
    return { filePath: exact, resolvedSuffix: suffix };
  }
  const extensions = [".js", ".mjs", ".cjs"];
  for (const ext of extensions) {
    const candidate = `${exact}${ext}`;
    if (isExistingFile(candidate)) {
      return { filePath: candidate, resolvedSuffix: `${suffix}${ext}` };
    }
  }
  for (const ext of extensions) {
    const candidate = resolve(exact, `index${ext}`);
    if (isExistingFile(candidate)) {
      const trimmed = suffix.replace(/\/+$/, "");
      return { filePath: candidate, resolvedSuffix: `${trimmed}/index${ext}` };
    }
  }
  return { filePath: exact, resolvedSuffix: suffix };
}

function isExistingFile(path) {
  if (!existsSync(path)) {
    return false;
  }
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
}

function resolvePackageMountRoot(packageName, { packageRoots, packagesRoot, filePath }) {
  // Walk up the package's `subpath` levels from `filePath` to find the
  // package directory root. Using `package_tree.resolvePackageSubpath`
  // already verified the file is within the package; this just gives
  // us the root for relative-import sandboxing.
  if (packageRoots && packageRoots[packageName]) {
    return packageRoots[packageName];
  }
  if (packagesRoot) {
    return resolve(packagesRoot, packageName);
  }
  // Fall back to deriving from filePath (less precise, but covers
  // tests that synthesize package_roots themselves).
  return dirname(filePath);
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
  if (typeof entry?.entry_file !== "string" || entry.entry_file === "") {
    throw new Error(`Vendor resolution for ${chunkPath} is missing entry_file in ${manifestPath}`);
  }
  return normalizeEntryFile(entry.entry_file);
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
