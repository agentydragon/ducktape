import { existsSync, readFileSync, realpathSync } from "node:fs";
import { isAbsolute, join, relative, resolve } from "node:path";

function defaultPackagesRoot() {
  for (const runfilesDir of [process.env.RUNFILES_DIR, process.env.TEST_SRCDIR]) {
    if (!runfilesDir) {
      continue;
    }
    const candidate = join(runfilesDir, "_main", "node_modules");
    if (existsSync(candidate)) {
      return candidate;
    }
  }
  throw new Error(
    "Could not locate Bazel-provided package tree in runfiles; pass packagesRoot explicitly for tests/fixtures"
  );
}

export function readInstalledPackageMetadata(packageName, { packageRoot, packageRoots, packagesRoot } = {}) {
  const resolvedPackageRoot = packageRoot ?? resolvePackageRoot(packageName, { packageRoots, packagesRoot });
  const metadataPath = join(resolvedPackageRoot, "package.json");
  if (!existsSync(metadataPath)) {
    throw new Error(`Package metadata missing for ${packageName}: ${metadataPath}`);
  }
  return JSON.parse(readFileSync(metadataPath, "utf8"));
}

function resolvePackageRoot(packageName, { packageRoots, packagesRoot } = {}) {
  const mappedRoot = packageRoots?.[packageName];
  if (mappedRoot !== undefined) {
    const resolvedPackageRoot = resolve(mappedRoot);
    if (!existsSync(resolvedPackageRoot)) {
      throw new Error(`Package root not found for ${packageName}: ${resolvedPackageRoot}`);
    }
    return resolvedPackageRoot;
  }
  if (packageRoots && packagesRoot === undefined) {
    throw new Error(`Package root not provided for ${packageName}`);
  }
  const resolvedPackagesRoot = resolve(packagesRoot ?? defaultPackagesRoot());
  const packageSegments = packagePathSegments(packageName);
  const packageRoot = resolve(resolvedPackagesRoot, ...packageSegments);
  assertPathWithinRoot(packageRoot, resolvedPackagesRoot, `Package ${packageName} escapes packages root`);
  if (!existsSync(packageRoot)) {
    throw new Error(`Package root not found for ${packageName}: ${packageRoot}`);
  }
  return packageRoot;
}

export function resolvePackageSubpath(packageName, subpath, { packageRoot, packageRoots, packagesRoot } = {}) {
  const resolvedPackageRoot = packageRoot ?? resolvePackageRoot(packageName, { packageRoots, packagesRoot });
  const filePath = resolve(resolvedPackageRoot, subpath);
  assertPathWithinRoot(
    filePath,
    resolvedPackageRoot,
    `Package ${packageName} subpath escapes package root: ${subpath}`
  );
  if (!existsSync(filePath)) {
    throw new Error(`Package file not found for ${packageName}: ${subpath} -> ${filePath}`);
  }
  assertRealPathWithinRoot(
    filePath,
    resolvedPackageRoot,
    `Package ${packageName} subpath realpath escapes package root: ${subpath}`
  );
  return filePath;
}

export function assertRealPathWithinRoot(path, root, message) {
  const realPath = realpathSync(path);
  const realRoot = realpathSync(root);
  assertPathWithinRoot(realPath, realRoot, message);
  return realPath;
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

function packagePathSegments(packageName) {
  if (typeof packageName !== "string" || packageName === "") {
    throw new Error(`Invalid package name: ${packageName}`);
  }
  const segments = packageName.split("/");
  if (segments.some((segment) => segment === "" || segment === "." || segment === "..")) {
    throw new Error(`Invalid package name: ${packageName}`);
  }
  return segments;
}
