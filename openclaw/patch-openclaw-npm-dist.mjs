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

function collectJavaScriptFiles(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const pathname = path.join(directory, entry.name);
    if (entry.isDirectory()) return collectJavaScriptFiles(pathname);
    return entry.isFile() && (entry.name.endsWith(".js") || entry.name.endsWith(".mjs")) ? [pathname] : [];
  });
}

const jsFiles = collectJavaScriptFiles(distDir);

// OpenClaw 2026.9.4 hard-codes its non-batch embedding concurrency and retry
// delays after retiring the configuration key that used to tune concurrency.
// Expose fail-fast environment overrides while retaining the upstream defaults
// for deployments that do not set them.
const memoryManagerFile = path.join(distDir, "extensions", "memory-core", "manager-runtime.js");
if (!fs.existsSync(memoryManagerFile)) {
  fail(`OpenClaw memory manager missing: ${memoryManagerFile}`);
}
let memoryManagerSource = fs.readFileSync(memoryManagerFile, "utf8");
const resolvePositiveIntegerEnvironmentVariable = `function resolvePositiveIntegerEnvironmentVariable(name, fallback) {
\tconst raw = process.env[name]?.trim();
\tif (!raw) return fallback;
\tconst value = Number(raw);
\tif (!Number.isSafeInteger(value) || value <= 0) throw new Error(\`\${name} must be a positive integer\`);
\treturn value;
}`;
const concurrencySetting = "const EMBEDDING_INDEX_CONCURRENCY = 4;";
if (memoryManagerSource.split(concurrencySetting).length - 1 !== 1) {
  fail(`expected exactly one OpenClaw memory manager setting: ${concurrencySetting}`);
}
memoryManagerSource = memoryManagerSource.replace(
  concurrencySetting,
  `${resolvePositiveIntegerEnvironmentVariable}
const EMBEDDING_INDEX_CONCURRENCY = resolvePositiveIntegerEnvironmentVariable("OPENCLAW_MEMORY_INDEX_CONCURRENCY", 4);`
);

const retryBudget = `const SHORT_MEMORY_EMBEDDING_RETRY_BUDGET = {
\tattempts: 3,
\tbaseDelayMs: 500,
\tmaxDelayMs: 8e3
};`;
if (memoryManagerSource.includes(retryBudget)) {
  const retryBudgetWithEnvironmentOverrides = `const OPENCLAW_MEMORY_RETRY_BASE_DELAY_MS = resolvePositiveIntegerEnvironmentVariable("OPENCLAW_MEMORY_RETRY_BASE_DELAY_MS", 500);
const OPENCLAW_MEMORY_RETRY_MAX_DELAY_MS = resolvePositiveIntegerEnvironmentVariable("OPENCLAW_MEMORY_RETRY_MAX_DELAY_MS", 8e3);
if (OPENCLAW_MEMORY_RETRY_MAX_DELAY_MS < OPENCLAW_MEMORY_RETRY_BASE_DELAY_MS) throw new Error("OPENCLAW_MEMORY_RETRY_MAX_DELAY_MS must be at least OPENCLAW_MEMORY_RETRY_BASE_DELAY_MS");
const SHORT_MEMORY_EMBEDDING_RETRY_BUDGET = {
\tattempts: 3,
\tbaseDelayMs: OPENCLAW_MEMORY_RETRY_BASE_DELAY_MS,
\tmaxDelayMs: OPENCLAW_MEMORY_RETRY_MAX_DELAY_MS
};`;
  if (memoryManagerSource.split(retryBudget).length - 1 !== 1) {
    fail("expected exactly one OpenClaw memory retry budget");
  }
  memoryManagerSource = memoryManagerSource.replace(retryBudget, retryBudgetWithEnvironmentOverrides);
} else {
  const retryBaseDelay = "const EMBEDDING_RETRY_BASE_DELAY_MS = 500;";
  const retryMaxDelay = "const EMBEDDING_RETRY_MAX_DELAY_MS = 8e3;";
  for (const [original, replacement] of [
    [
      retryBaseDelay,
      'const EMBEDDING_RETRY_BASE_DELAY_MS = resolvePositiveIntegerEnvironmentVariable("OPENCLAW_MEMORY_RETRY_BASE_DELAY_MS", 500);',
    ],
    [
      retryMaxDelay,
      `const EMBEDDING_RETRY_MAX_DELAY_MS = resolvePositiveIntegerEnvironmentVariable("OPENCLAW_MEMORY_RETRY_MAX_DELAY_MS", 8e3);
if (EMBEDDING_RETRY_MAX_DELAY_MS < EMBEDDING_RETRY_BASE_DELAY_MS) throw new Error("OPENCLAW_MEMORY_RETRY_MAX_DELAY_MS must be at least OPENCLAW_MEMORY_RETRY_BASE_DELAY_MS");`,
    ],
  ]) {
    if (memoryManagerSource.split(original).length - 1 !== 1) {
      fail(`expected exactly one OpenClaw memory manager setting: ${original}`);
    }
    memoryManagerSource = memoryManagerSource.replace(original, replacement);
  }
}
fs.writeFileSync(memoryManagerFile, memoryManagerSource);

// OpenClaw 2026.9.4 performs a full O(N log N) SQLite integrity_check while
// opening every already-current agent database. On rotational state storage,
// that synchronous scan can outlive the five-minute startup migration lease
// and prevent its event-loop heartbeat from running. Allow gateways to select
// SQLite's O(N) quick_check (or skip the pragma entirely) for this steady-state
// startup path while retaining the upstream full check by default and for
// doctor/backup/compaction commands.
const sqliteIntegrityFiles = jsFiles.filter((file) => {
  const source = fs.readFileSync(file, "utf8");
  return (
    source.includes("function assertSqliteIntegrity(database, databaseLabel)") &&
    source.includes("function runSqliteCheck(database, databaseLabel, pragma, tableName)") &&
    source.includes("assertSqliteIntegrity as t")
  );
});
if (sqliteIntegrityFiles.length !== 1) {
  fail(`expected exactly one SQLite integrity chunk, found ${sqliteIntegrityFiles.length}`);
}
const sqliteIntegrityFile = sqliteIntegrityFiles[0];
let sqliteIntegritySource = fs.readFileSync(sqliteIntegrityFile, "utf8");
const fullIntegrityFunction = `function assertSqliteIntegrity(database, databaseLabel) {
\tconst integrityCheck = runSqliteCheck(database, databaseLabel, "integrity_check");
\trunSqliteForeignKeyCheck(database, databaseLabel);
\treturn { integrityCheck };
}`;
const quickIntegrityFunction = `function assertSqliteQuickIntegrity(database, databaseLabel) {
\tconst integrityCheck = runSqliteCheck(database, databaseLabel, "quick_check");
\trunSqliteForeignKeyCheck(database, databaseLabel);
\treturn { integrityCheck };
}`;
if (sqliteIntegritySource.split(fullIntegrityFunction).length - 1 !== 1) {
  fail("SQLite integrity chunk did not contain exactly one full integrity function");
}
sqliteIntegritySource = sqliteIntegritySource.replace(
  fullIntegrityFunction,
  `${fullIntegrityFunction}\n${quickIntegrityFunction}`
);
const sqliteIntegrityExport = "assertSqliteIntegrity as t";
if (sqliteIntegritySource.split(sqliteIntegrityExport).length - 1 !== 1) {
  fail("SQLite integrity chunk did not contain exactly one expected export list");
}
sqliteIntegritySource = sqliteIntegritySource.replace(
  sqliteIntegrityExport,
  "assertSqliteQuickIntegrity as a, assertSqliteIntegrity as t"
);
fs.writeFileSync(sqliteIntegrityFile, sqliteIntegritySource);

const agentDatabaseMaintenanceFiles = jsFiles.filter(
  (file) =>
    fs
      .readFileSync(file, "utf8")
      .includes("function assertAgentDatabaseIntegrityBeforeMutation(database, agentId, pathname)") ||
    fs
      .readFileSync(file, "utf8")
      .includes("function* agentDatabaseIntegrityBeforeMutationSteps(database, agentId, pathname, diagnostics)")
);
if (agentDatabaseMaintenanceFiles.length !== 1) {
  fail(`expected exactly one agent database maintenance chunk, found ${agentDatabaseMaintenanceFiles.length}`);
}
const agentDatabaseMaintenanceFile = agentDatabaseMaintenanceFiles[0];
let agentDatabaseMaintenanceSource = fs.readFileSync(agentDatabaseMaintenanceFile, "utf8");
const sqliteIntegrityModule = `./${path.basename(sqliteIntegrityFile)}`;
const agentDatabaseIntegrityImportLines = agentDatabaseMaintenanceSource
  .split("\n")
  .filter(
    (line) =>
      line.startsWith("import ") &&
      line.includes(`from "${sqliteIntegrityModule}";`) &&
      line.includes("t as assertSqliteIntegrity")
  );
if (agentDatabaseIntegrityImportLines.length !== 1) {
  fail("agent database maintenance chunk did not contain exactly one SQLite integrity import");
}
agentDatabaseMaintenanceSource = agentDatabaseMaintenanceSource.replace(
  agentDatabaseIntegrityImportLines[0],
  agentDatabaseIntegrityImportLines[0].replace(
    "t as assertSqliteIntegrity",
    "a as assertSqliteQuickIntegrity, t as assertSqliteIntegrity"
  )
);
const legacyAgentDatabaseIntegrityBranch = `\tif (userVersion === 19 && !hasPendingCurrentVersionMigration) verifyAndRepairCanonicalSqliteIndexes(database, pathname, OPENCLAW_AGENT_SCHEMA_SQL, {
\t\tallowMissingColumns: true,
\t\tvalidateAfterRepair: () => assertOpenClawAgentCurrentRuntimeSchema(database, {
\t\t\tagentId,
\t\t\tpathname
\t\t})
\t});
\telse assertSqliteIntegrity(database, pathname);`;
const patchedAgentDatabaseIntegrityBranch = `\tif (userVersion === 19 && !hasPendingCurrentVersionMigration) {
\t\tconst configuredIntegrityCheck = process.env.OPENCLAW_AGENT_DB_STARTUP_INTEGRITY_CHECK?.trim().toLowerCase() || "full";
\t\tif (configuredIntegrityCheck !== "full" && configuredIntegrityCheck !== "quick" && configuredIntegrityCheck !== "none") throw new Error("OPENCLAW_AGENT_DB_STARTUP_INTEGRITY_CHECK must be full, quick, or none");
\t\tconst indexRepairOptions = {
\t\t\tallowMissingColumns: true,
\t\t\tvalidateAfterRepair: () => assertOpenClawAgentCurrentRuntimeSchema(database, {
\t\t\t\tagentId,
\t\t\t\tpathname
\t\t\t})
\t\t};
\t\tif (configuredIntegrityCheck === "full") verifyAndRepairCanonicalSqliteIndexes(database, pathname, OPENCLAW_AGENT_SCHEMA_SQL, indexRepairOptions);
\t\telse {
\t\t\tif (configuredIntegrityCheck === "quick") assertSqliteQuickIntegrity(database, pathname);
\t\t\trepairCanonicalSqliteIndexes(database, pathname, OPENCLAW_AGENT_SCHEMA_SQL, {
\t\t\t\t...indexRepairOptions,
\t\t\t\tverifyPhysicalIntegrity: false
\t\t\t});
\t\t}
\t} else assertSqliteIntegrity(database, pathname);`;
const currentAgentDatabaseIntegrityBranch = `\tif (userVersion === 19 && !hasPendingCurrentVersionMigration) {
\t\tyield* verifyAndRepairCanonicalSqliteIndexSteps(database, pathname, OPENCLAW_AGENT_SCHEMA_SQL, {
\t\t\tallowMissingColumns: true,
\t\t\tvalidateAfterRepair: () => assertOpenClawAgentCurrentRuntimeSchema(database, {
\t\t\t\tagentId,
\t\t\t\tpathname
\t\t\t}),
\t\t\tdiagnostics
\t\t});
\t\tassertOpenClawAgentCurrentRuntimeSchema(database, {
\t\t\tagentId,
\t\t\tpathname
\t\t});
\t} else if (userVersion === 0 && !hasApplicationSchema && database.prepare("PRAGMA page_count").get()?.page_count === 0) assertSqliteIntegrity(database, pathname);
\telse yield* sqliteIntegrityCheckSteps(database, pathname, diagnostics);`;
const patchedCurrentAgentDatabaseIntegrityBranch = `\tif (userVersion === 19 && !hasPendingCurrentVersionMigration) {
\t\tconst configuredIntegrityCheck = process.env.OPENCLAW_AGENT_DB_STARTUP_INTEGRITY_CHECK?.trim().toLowerCase() || "full";
\t\tif (configuredIntegrityCheck !== "full" && configuredIntegrityCheck !== "quick" && configuredIntegrityCheck !== "none") throw new Error("OPENCLAW_AGENT_DB_STARTUP_INTEGRITY_CHECK must be full, quick, or none");
\t\tconst indexRepairOptions = {
\t\t\tallowMissingColumns: true,
\t\t\tvalidateAfterRepair: () => assertOpenClawAgentCurrentRuntimeSchema(database, {
\t\t\t\tagentId,
\t\t\t\tpathname
\t\t\t})
\t\t};
\t\tif (configuredIntegrityCheck === "full") yield* verifyAndRepairCanonicalSqliteIndexSteps(database, pathname, OPENCLAW_AGENT_SCHEMA_SQL, {
\t\t\t...indexRepairOptions,
\t\t\tdiagnostics
\t\t});
\t\telse {
\t\t\tif (configuredIntegrityCheck === "quick") assertSqliteQuickIntegrity(database, pathname);
\t\t\trepairCanonicalSqliteIndexes(database, pathname, OPENCLAW_AGENT_SCHEMA_SQL, {
\t\t\t\t...indexRepairOptions,
\t\t\t\tverifyPhysicalIntegrity: false
\t\t\t});
\t\t}
\t\tassertOpenClawAgentCurrentRuntimeSchema(database, {
\t\t\tagentId,
\t\t\tpathname
\t\t});
\t} else if (userVersion === 0 && !hasApplicationSchema && database.prepare("PRAGMA page_count").get()?.page_count === 0) assertSqliteIntegrity(database, pathname);
\telse yield* sqliteIntegrityCheckSteps(database, pathname, diagnostics);`;
const legacyAgentDatabaseIntegrityCount =
  agentDatabaseMaintenanceSource.split(legacyAgentDatabaseIntegrityBranch).length - 1;
const currentAgentDatabaseIntegrityCount =
  agentDatabaseMaintenanceSource.split(currentAgentDatabaseIntegrityBranch).length - 1;
if (legacyAgentDatabaseIntegrityCount === 1 && currentAgentDatabaseIntegrityCount === 0) {
  agentDatabaseMaintenanceSource = agentDatabaseMaintenanceSource.replace(
    legacyAgentDatabaseIntegrityBranch,
    patchedAgentDatabaseIntegrityBranch
  );
} else if (legacyAgentDatabaseIntegrityCount === 0 && currentAgentDatabaseIntegrityCount === 1) {
  agentDatabaseMaintenanceSource = agentDatabaseMaintenanceSource.replace(
    currentAgentDatabaseIntegrityBranch,
    patchedCurrentAgentDatabaseIntegrityBranch
  );
} else {
  fail("agent database maintenance chunk did not contain exactly one supported current-schema integrity branch");
}
fs.writeFileSync(agentDatabaseMaintenanceFile, agentDatabaseMaintenanceSource);

// OpenClaw 2026.9.4 runs a background database integrity verifier that forks a
// worker, copies every registered agent database out of the state volume, and
// scans the copy. Its five-minute initial delay and 24-hour repeat are module
// constants despite their environment-variable-shaped names, and its only guard
// is a vitest-only test flag -- so a gateway that restarts more often than daily
// pays the copy-and-scan on every start and never reaches the cadence the check
// was designed around. Add the off switch upstream lacks, defaulting to on.
const databaseVerifyFiles = jsFiles.filter((file) => {
  const source = fs.readFileSync(file, "utf8");
  return (
    source.includes("function startOpenClawDatabaseIntegrityVerifier(options)") &&
    (source.includes("const OPENCLAW_DATABASE_VERIFY_INTERVAL_MS = 1440 * 6e4;") ||
      source.includes("const OPENCLAW_DATABASE_VERIFY_INTERVAL_MS = 864e5;"))
  );
});
if (databaseVerifyFiles.length !== 1) {
  fail(`expected exactly one database verify chunk, found ${databaseVerifyFiles.length}`);
}
const databaseVerifyFile = databaseVerifyFiles[0];
let databaseVerifySource = fs.readFileSync(databaseVerifyFile, "utf8");
const databaseVerifyInterval = databaseVerifySource.includes("const OPENCLAW_DATABASE_VERIFY_INTERVAL_MS = 864e5;")
  ? "const OPENCLAW_DATABASE_VERIFY_INTERVAL_MS = 864e5;"
  : "const OPENCLAW_DATABASE_VERIFY_INTERVAL_MS = 1440 * 6e4;";
const databaseVerifyReplacements = new Map([
  [
    databaseVerifyInterval,
    `${databaseVerifyInterval}
function resolveOpenClawDatabaseVerifyEnabled() {
\tconst raw = process.env.OPENCLAW_DATABASE_VERIFY?.trim().toLowerCase() || "on";
\tif (raw !== "on" && raw !== "off") throw new Error("OPENCLAW_DATABASE_VERIFY must be on or off");
\treturn raw === "on";
}`,
  ],
  [
    "\tschedule(OPENCLAW_DATABASE_VERIFY_INITIAL_DELAY_MS);",
    `\tif (resolveOpenClawDatabaseVerifyEnabled()) schedule(OPENCLAW_DATABASE_VERIFY_INITIAL_DELAY_MS);
\telse log.info("database integrity verifier disabled by OPENCLAW_DATABASE_VERIFY=off");`,
  ],
]);
for (const [original, replacement] of databaseVerifyReplacements) {
  if (databaseVerifySource.split(original).length - 1 !== 1) {
    fail(`expected exactly one OpenClaw database verify site: ${original}`);
  }
  databaseVerifySource = databaseVerifySource.replace(original, replacement);
}
fs.writeFileSync(databaseVerifyFile, databaseVerifySource);

const hardlinkPolicyFiles = jsFiles.filter((file) =>
  fs.readFileSync(file, "utf8").includes("function shouldRejectHardlinkedPluginFiles")
);

if (hardlinkPolicyFiles.length === 0) {
  fail("expected at least one bundled hardlink policy chunk");
}

// OpenClaw 2026.9.4 contains the Nix-store hardlink exception natively, but
// its minified source changed the realpath helper signature. Keep accepting
// the older patched shape for compatibility with older package pins while
// avoiding a second, incompatible ownership rewrite on newer dist files.
const nativeNixStoreHardlinkException = "resolveIsNixMode(params.env) && isNixStorePluginRoot(params.rootDir)";
const patchedNixStoreHardlinkException =
  "resolveIsNixMode(params.env) && isNixStorePluginRoot(params.rootDir, params.realpathCache)";
for (const hardlinkPolicyFile of hardlinkPolicyFiles) {
  let hardlinkSource = fs.readFileSync(hardlinkPolicyFile, "utf8");
  const hasNixStoreHardlinkException =
    hardlinkSource.includes(nativeNixStoreHardlinkException) ||
    hardlinkSource.includes(patchedNixStoreHardlinkException) ||
    hardlinkSource.includes("isTrustedNixStorePluginRoot(params)") ||
    /resolveIsNixMode\([^)]*\)\s*&&\s*isNixStorePluginRoot\(/.test(hardlinkSource);
  if (!hasNixStoreHardlinkException) {
    fail(`OpenClaw hardlink policy chunk did not contain the expected Nix store exception: ${hardlinkPolicyFile}`);
  }

  // Older OpenClaw dists needed a discovery ownership rewrite as well. Stable
  // 2026.9.4 marks bundled extension roots explicitly and already handles the
  // Nix-store exception in its hardlink policy, so leave its changed discovery
  // chunk untouched. This is deliberately fail-closed for older shapes.
  if (
    !hardlinkSource.includes(nativeNixStoreHardlinkException) &&
    !/resolveIsNixMode\([^)]*\)\s*&&\s*isNixStorePluginRoot\(/.test(hardlinkSource)
  ) {
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
