#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

function fail(message) {
  console.error(message);
  process.exit(1);
}

const root = process.env.OPENCLAW_PACKAGE_ROOT;
if (!root) {
  fail("OPENCLAW_PACKAGE_ROOT is required");
}

const distDir = path.join(root, "dist");
if (!fs.existsSync(distDir)) {
  fail(`OpenClaw dist directory missing: ${distDir}`);
}

const jsFiles = fs
  .readdirSync(distDir)
  .filter((name) => name.endsWith(".js"))
  .map((name) => path.join(distDir, name));

// OpenClaw 2026.8.1 hard-codes its non-batch embedding concurrency and retry
// delays after retiring the configuration key that used to tune concurrency.
// Expose fail-fast environment overrides while retaining the upstream defaults
// for deployments that do not set them.
const memoryManagerFile = path.join(distDir, "extensions", "memory-core", "manager-runtime.js");
if (!fs.existsSync(memoryManagerFile)) {
  fail(`OpenClaw memory manager missing: ${memoryManagerFile}`);
}
let memoryManagerSource = fs.readFileSync(memoryManagerFile, "utf8");
const memoryManagerReplacements = new Map([
  [
    "const EMBEDDING_INDEX_CONCURRENCY = 4;",
    `function resolvePositiveIntegerEnvironmentVariable(name, fallback) {
\tconst raw = process.env[name]?.trim();
\tif (!raw) return fallback;
\tconst value = Number(raw);
\tif (!Number.isSafeInteger(value) || value <= 0) throw new Error(\`\${name} must be a positive integer\`);
\treturn value;
}
const EMBEDDING_INDEX_CONCURRENCY = resolvePositiveIntegerEnvironmentVariable("OPENCLAW_MEMORY_INDEX_CONCURRENCY", 4);`,
  ],
  [
    "const EMBEDDING_RETRY_BASE_DELAY_MS = 500;",
    'const EMBEDDING_RETRY_BASE_DELAY_MS = resolvePositiveIntegerEnvironmentVariable("OPENCLAW_MEMORY_RETRY_BASE_DELAY_MS", 500);',
  ],
  [
    "const EMBEDDING_RETRY_MAX_DELAY_MS = 8e3;",
    `const EMBEDDING_RETRY_MAX_DELAY_MS = resolvePositiveIntegerEnvironmentVariable("OPENCLAW_MEMORY_RETRY_MAX_DELAY_MS", 8e3);
if (EMBEDDING_RETRY_MAX_DELAY_MS < EMBEDDING_RETRY_BASE_DELAY_MS) throw new Error("OPENCLAW_MEMORY_RETRY_MAX_DELAY_MS must be at least OPENCLAW_MEMORY_RETRY_BASE_DELAY_MS");`,
  ],
]);
for (const [original, replacement] of memoryManagerReplacements) {
  if (memoryManagerSource.split(original).length - 1 !== 1) {
    fail(`expected exactly one OpenClaw memory manager setting: ${original}`);
  }
  memoryManagerSource = memoryManagerSource.replace(original, replacement);
  if (!memoryManagerSource.includes(replacement)) {
    fail(`OpenClaw memory manager did not receive setting: ${replacement}`);
  }
}
fs.writeFileSync(memoryManagerFile, memoryManagerSource);

const hardlinkPolicyFiles = jsFiles.filter((file) =>
  fs.readFileSync(file, "utf8").includes("function shouldRejectHardlinkedPluginFiles")
);

if (hardlinkPolicyFiles.length !== 1) {
  fail(`expected exactly one bundled hardlink policy chunk, found ${hardlinkPolicyFiles.length}`);
}

const hardlinkPolicyFile = hardlinkPolicyFiles[0];
const hardlinkSource = fs.readFileSync(hardlinkPolicyFile, "utf8");

// OpenClaw 2026.8.1 contains the Nix-store hardlink exception natively, but
// its minified source changed the realpath helper signature. Keep accepting
// the older patched shape for compatibility with older package pins while
// avoiding a second, incompatible ownership rewrite on newer dist files.
const nativeNixStoreHardlinkException = "resolveIsNixMode(params.env) && isNixStorePluginRoot(params.rootDir)";
const patchedNixStoreHardlinkException =
  "resolveIsNixMode(params.env) && isNixStorePluginRoot(params.rootDir, params.realpathCache)";
const hasNixStoreHardlinkException =
  hardlinkSource.includes(nativeNixStoreHardlinkException) ||
  hardlinkSource.includes(patchedNixStoreHardlinkException) ||
  hardlinkSource.includes("isTrustedNixStorePluginRoot(params)");
if (!hasNixStoreHardlinkException) {
  fail("OpenClaw hardlink policy chunk did not contain the expected Nix store exception");
}

// Older OpenClaw dists needed a discovery ownership rewrite as well. Stable
// 2026.8.1 marks bundled extension roots explicitly and already handles the
// Nix-store exception in its hardlink policy, so leave its changed discovery
// chunk untouched. This is deliberately fail-closed for older shapes.
if (!hardlinkSource.includes(nativeNixStoreHardlinkException)) {
  const ownershipCheck =
    'params.origin !== "bundled" && params.uid !== null && typeof stat.uid === "number" && stat.uid !== params.uid && stat.uid !== 0';
  const patchedOwnershipCheck =
    'params.origin !== "bundled" && params.uid !== null && !isTrustedNixStorePluginRoot(params) && typeof stat.uid === "number" && stat.uid !== params.uid && stat.uid !== 0';
  const ownershipFiles = jsFiles.filter((file) => {
    const source = fs.readFileSync(file, "utf8");
    return source.includes(ownershipCheck) || source.includes(patchedOwnershipCheck);
  });

  if (ownershipFiles.length !== 1) {
    fail(`expected exactly one bundled ownership policy chunk, found ${ownershipFiles.length}`);
  }

  const ownershipFile = ownershipFiles[0];
  let source = fs.readFileSync(ownershipFile, "utf8");

  if (!source.includes(patchedOwnershipCheck)) {
    if (!source.includes(ownershipCheck)) {
      fail("OpenClaw discovery chunk did not contain the expected ownership check");
    }
    if (!source.includes("function isTrustedNixStorePluginRoot")) {
      if (!source.includes("safeRealpathSync") || !source.includes('path from "node:path"')) {
        fail("OpenClaw ownership chunk is missing imports required for the Nix store ownership patch");
      }
      source = source.replace(
        /^(?:import [^\n]+;\n)+/,
        `$&const NIX_STORE_PLUGIN_OWNERSHIP_ROOT = "/nix/store";
function isTrustedNixStorePluginRoot(params) {
\tconst rootRealPath = safeRealpathSync(params.rootDir, params.realpathCache) ?? path.resolve(params.rootDir);
\treturn (params.env ?? process.env).OPENCLAW_NIX_MODE === "1" && (rootRealPath === NIX_STORE_PLUGIN_OWNERSHIP_ROOT || rootRealPath.startsWith(\`${NIX_STORE_PLUGIN_OWNERSHIP_ROOT}/\`));
}
`
      );
    }
    source = source.replace(ownershipCheck, patchedOwnershipCheck);
  }

  if (!source.includes("function isTrustedNixStorePluginRoot")) {
    fail("OpenClaw ownership chunk did not receive the Nix store trust helper");
  }
  if (!source.includes(patchedOwnershipCheck)) {
    fail("OpenClaw ownership chunk did not receive the Nix store ownership patch");
  }

  fs.writeFileSync(ownershipFile, source);
}

const missingConfiguredInstallLoop = "for (const candidate of collectDownloadableInstallCandidates({";
const legacyPatchedMissingConfiguredInstallLoop =
  'if (env.OPENCLAW_NIX_MODE !== "1") for (const candidate of collectDownloadableInstallCandidates({';
const patchedMissingConfiguredInstallLoop =
  'if ((params.env ?? process.env).OPENCLAW_NIX_MODE !== "1") for (const candidate of collectDownloadableInstallCandidates({';

const missingConfiguredInstallFiles = jsFiles.filter((file) => {
  const candidate = fs.readFileSync(file, "utf8");
  return (
    candidate.includes('Failed to install missing configured plugin "') &&
    (candidate.includes(missingConfiguredInstallLoop) || candidate.includes(patchedMissingConfiguredInstallLoop))
  );
});

if (missingConfiguredInstallFiles.length !== 1) {
  fail(`expected exactly one missing configured plugin install chunk, found ${missingConfiguredInstallFiles.length}`);
}

const missingConfiguredInstallFile = missingConfiguredInstallFiles[0];
let missingConfiguredInstallSource = fs.readFileSync(missingConfiguredInstallFile, "utf8");

const normalizedMissingConfiguredInstallSource = missingConfiguredInstallSource.replaceAll(
  legacyPatchedMissingConfiguredInstallLoop,
  missingConfiguredInstallLoop
);
const missingConfiguredInstallLoopCount =
  normalizedMissingConfiguredInstallSource.split(missingConfiguredInstallLoop).length - 1;
missingConfiguredInstallSource = normalizedMissingConfiguredInstallSource.replaceAll(
  missingConfiguredInstallLoop,
  patchedMissingConfiguredInstallLoop
);

const patchedMissingConfiguredInstallLoopCount =
  missingConfiguredInstallSource.split(patchedMissingConfiguredInstallLoop).length - 1;
if (missingConfiguredInstallLoopCount === 0) {
  fail("OpenClaw missing configured plugin install chunk did not contain an auto-install candidate loop");
}
if (patchedMissingConfiguredInstallLoopCount !== missingConfiguredInstallLoopCount) {
  fail("OpenClaw missing configured plugin install chunk did not receive the Nix mode auto-install guard");
}
if (missingConfiguredInstallSource.includes(legacyPatchedMissingConfiguredInstallLoop)) {
  fail("OpenClaw missing configured plugin install chunk still has the legacy Nix mode auto-install guard");
}

fs.writeFileSync(missingConfiguredInstallFile, missingConfiguredInstallSource);
