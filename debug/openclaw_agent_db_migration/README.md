# OpenClaw 2026.8.1 bump: Public Coder startup refusal

Two independent bugs, both triggered by the 2026.8.1 image (#5369). The first is
resolved; the second needs an image rollout.

## 1. Agent identity migration could not run (RESOLVED)

`autoMigrateLegacyState` runs `migrateLegacyMediaPersistence` (gated on
`doctorOnlyStateMigrations`, i.e. `doctor --fix`, holding the agent-database
maintenance lease) and then `migrateHistoricalTranscriptDirectives`, which is
**not** gated and takes no lease. The second needs the schema the first advances.

On gateway startup only the second runs, so `ensureAgentSchema` hits

```js
const identityMigration = targetVersion >= 18 && readSqliteUserVersion(db) < targetVersion && ...;
if (identityMigration) assertAgentDatabaseMaintenanceAuthority();
```

with no `AsyncLocalStorage` authority — an unconditional throw for any agent
database below schema 19. `completeStartupMigrationPreflight` turns the resulting
warning into `ExitError(1)`. The error text ("stop active agents and run openclaw
doctor --fix") is misleading: nothing holds the database, and the gateway is the
only process running.

`doctor --fix` cannot be the escape hatch here, because
`assertConfigWriteAllowedInCurrentMode` aborts the whole repair flow whenever
`OPENCLAW_NIX_MODE=1`, which nix-openclaw's wrapper sets. Gateway refuses to boot
until the migration runs; the only tool that runs it refuses to start. The image
cannot self-heal.

Resolved by `maintenance-job.yaml`, which calls the gateway's own exported
`withAgentDatabaseMaintenanceLease` + `migrateOpenClawAgentDatabaseForMaintenance`
per database. Both agent databases went `user_version` 1 -> 19, and the gateway's
own next startup completed the transcript-directive phase (its `schema_meta` row
now reads `{"phase":"complete"}`).

**Gotcha:** take one lease per database. The migration is fully synchronous, so it
starves the lease heartbeat's timer; a database large enough to block past the 60s
TTL leaves the lease lost for every database after it.

Pre-migration copies are on the PVC at `.maintenance-backup-pre-2026.8.1/`
(145 MB); delete once the upgrade is trusted. The newest restic snapshot predating
the bump is `7a0669a5` (09:17 UTC 2026-09-03).

## 2. `dist-runtime` is missing its shared chunks (FIX PENDING ROLLOUT)

nix-openclaw's `stage_dist_runtime` copies `dist/extensions` into `dist-runtime/`
and nothing else, but extension modules import shared chunks as `../../<chunk>.js`
— resolving to `dist/` upstream and to `dist-runtime/` here, where only
`extensions/` exists. 496 of 526 extension files import such a chunk. OpenClaw
prefers `dist-runtime/extensions` when present, so the partial tree is worse than
none.

Mostly cosmetic (`canvas`, `file-transfer`, `ollama` doctor contracts fail to
load), except `workboard`: its contract is loaded by a legacy state migration, so
its `ERR_MODULE_NOT_FOUND` becomes a _blocking_ startup-migration warning. This was
hidden until bug 1 was fixed, because startup aborted earlier.

Fixed in `openclaw/gateway.nix`, a shared derivation both images now consume: it
symlinks the sibling `dist/` entries into `dist-runtime/` and then proves every
specifier the staged extensions import resolves.

**Gotcha:** append to `installPhase`, not `postInstall`. nix-openclaw supplies a
complete custom `installPhase` and never calls `runHook postInstall`, so a
`postInstall` is silently skipped -- the build succeeds having done nothing, and a
fail-closed guard placed there passes vacuously.

**Gotcha for the guard:** resolve each specifier against its own importer (a nested
extension file's `../../` means `extensions/`, not the tree root), scan `.js` only
(`.d.ts` references are type-level), match bare specifier strings -- workboard's is a
dynamic `import()`, so a `from "..."` pattern misses the one that broke us -- and skip
vendored `node_modules`, since `stage_acpx` splices in a plugin whose own packages
resolve through their own tree.

## Upstream

Both are upstream bugs worth reporting: the missing `doctorOnlyStateMigrations`
gate (openclaw), and `stage_dist_runtime` (nix-openclaw).

## Note

Manually suspending `public-coder-agent-app` does not hold — the Kustomization
object is itself reconciled by the `flux-system` Kustomization, which clears
`spec.suspend` and restores `replicas: 1`.
