# OpenClaw 2026.8.1: recovering an agent whose state predates the bump

The 2026.8.1 bump (#5369) took `public-coder-agent` down for ~5 hours through
four independent failures, each hidden behind the previous one. All four are
resolved for public-coder, and the runbook below has since recovered
`haku-openclaw-spike` as well — only failure (1) applied there, in six seconds,
because Flux had already rolled it onto an image carrying the (2) fix and its
state had no legacy session store.

Remaining public-coder cleanup:
<../../../cluster/k8s/agents/public-coder-agent/TODO.md>.

## The shape of the trap

Three of the four share one shape: **startup detects a migration and hard-fails,
only `doctor` performs it, and `doctor` refuses to run under Nix.**

`assertConfigWriteAllowedInCurrentMode` throws whenever `OPENCLAW_NIX_MODE=1`,
which nix-openclaw's wrapper sets, and `doctor --fix` calls it unconditionally
before touching any state:

```js
if (options.repair === true || options.yes === true || options.generateGatewayToken === true) {
  assertConfigWriteAllowedInCurrentMode(); // throws under OPENCLAW_NIX_MODE=1
}
```

So the gateway will not start until a migration runs, and the only tool that runs
it will not start either. **A Nix install carrying pre-2026.8.1 agent state cannot
self-heal.** Recovery means running `doctor` with `OPENCLAW_NIX_MODE=0`, or
driving the migration directly.

Gotcha: `OPENCLAW_NIX_MODE=""` does not work. The wrapper uses
`makeWrapper --set-default`, i.e. `${OPENCLAW_NIX_MODE:-1}`, so an empty value
falls back to `1`. Set it to `"0"`.

## 1. Agent identity migration (resolved)

`autoMigrateLegacyState` runs `migrateLegacyMediaPersistence` (gated on
`doctorOnlyStateMigrations`, holding the agent-database maintenance lease) and
then `migrateHistoricalTranscriptDirectives`, which is **not** gated and takes no
lease but needs the schema the first advances. On startup only the second runs:

```js
const identityMigration = targetVersion >= 18 && readSqliteUserVersion(db) < targetVersion && ...;
if (identityMigration) assertAgentDatabaseMaintenanceAuthority();
```

That asserts an `AsyncLocalStorage` authority nobody established — an
unconditional throw for any agent database below schema 19, which
`completeStartupMigrationPreflight` turns into `ExitError(1)`. The error text
("stop active agents") is misleading: nothing holds the database.

Fixed by `maintenance-job.yaml`, which calls the gateway's own exported
`withAgentDatabaseMaintenanceLease` + `migrateOpenClawAgentDatabaseForMaintenance`
per database. Both databases went `user_version` 1 -> 19, after which the
gateway's own startup completed the transcript-directive phase.

**Gotcha:** take one lease per database. The migration is fully synchronous, so it
starves the lease heartbeat's timer; a database large enough to block past the 60s
TTL leaves the lease lost for every database after it.

## 2. `dist-runtime` missing its shared chunks (resolved)

nix-openclaw's `stage_dist_runtime` copies `dist/extensions` into `dist-runtime/`
and nothing else, but extension modules import shared chunks as `../../<chunk>.js`
— resolving to `dist/` upstream and to `dist-runtime/` here, where only
`extensions/` exists. OpenClaw prefers `dist-runtime/extensions` when present, so
the partial tree is worse than none.

Mostly cosmetic, except `workboard`: its doctor contract is loaded by a legacy
state migration, so its `ERR_MODULE_NOT_FOUND` became a blocking
startup-migration warning. Invisible until (1) was fixed, because startup aborted
earlier.

Fixed in `openclaw/gateway.nix`, now shared by both images.

**Gotcha:** append to `installPhase`, not `postInstall`. nix-openclaw supplies a
complete custom `installPhase` and never calls `runHook postInstall`, so a
`postInstall` is silently skipped — the build succeeds having done nothing, and a
fail-closed guard placed there passes vacuously.

**Gotcha for the guard:** resolve each specifier against its own importer (a
nested extension file's `../../` means `extensions/`, not the tree root), scan
`.js` only (`.d.ts` references are type-level), match bare specifier strings —
workboard's is a dynamic `import()`, so a `from "..."` pattern misses it — and
skip vendored `node_modules`, since `stage_acpx` splices in a plugin whose own
packages resolve through their own tree.

## 3. Legacy session store (resolved)

Startup throws on any session store path that is not `.sqlite`:

```js
const legacyStore = [
  path.join(resolveStateDir(env), "sessions", "sessions.json"),
  ...targets.map((t) => t.storePath),
].find((p) => !p.endsWith(".sqlite") && fs.existsSync(p));
if (legacyStore) throw new Error(`Legacy session store requires migration: ${legacyStore}...`);
```

Only doctor migrates it (`repairSessionFiles` is gated on `isDoctor ||
doctorOnlyStateMigrations`). Two things had to be true before it would:

1. **`agents.defaults.sessionStore.agentId` must name the live owner.** A legacy
   `agents/main/` directory predates the multi-agent config, and OpenClaw will not
   guess which live agent inherits its sessions. Without it the migration reports
   "legacy main rows have no unambiguous configured owner"; with it, "deferred".
   Setting it to `coder` was enough. This was needed only for the migration — the
   ConfigMap reseed removed it afterwards and the gateway stayed healthy, so it
   was deliberately not landed in git.
2. **The state PVC root must be writable by uid 1000.** It was `root:root` from an
   earlier hand restore, so doctor died on `EPERM: operation not permitted,
fchmod` — _before_ reaching the deferred migration, and without naming the
   directory. `doctor-job.yaml` fixes this with a root init container doing a
   single non-recursive `chown 1000:1000`.

With both in place `doctor --fix` exited 0 and both `sessions.json` files
migrated. Do not delete a legacy store to get past the gate: the ambiguous rows
were ~259 KB of two `:main` keys, but the files held 357 sessions and ~24 MB, and
deleting the file loses all of it.

## 4. Control UI demanded device pairing (resolved)

`gateway.controlUi.dangerouslyDisableDeviceAuth` is retired in 2026.8.1 and
silently ignored, so pairing came back on despite Authentik authenticating the
browser. The tell is `phase=auth_validated` on the rejection:

```text
[ws] closed before connect ... code=1008 reason=pairing required: device is not approved yet
  phase=auth_validated
```

Replaced by `gateway.auth.trustedProxy.deviceAutoApprove.enabled` in #5514.

## Runbook for another instance

1. Scale the Deployment to 0 and confirm the pod is gone. Manually suspending the
   Flux Kustomization does **not** hold — the Kustomization object is itself
   reconciled by `flux-system`, which clears `spec.suspend` and restores
   `replicas: 1`. Work promptly or expect the pod back.
2. Run `maintenance-job.yaml` (agent identity migration, one lease per database).
   Both jobs here are shaped for public-coder; copy the target Deployment's own
   `imagePullSecrets`, PVC name, mount path and node placement rather than
   assuming these. `haku-openclaw-spike` differs on all four, and a missing
   `forgejo-images-creds` shows up only as a ten-minute `ImagePullBackOff`.
3. Run `doctor-job.yaml` (chown the PVC root, then `doctor --fix` with
   `OPENCLAW_NIX_MODE=0`). Add `agents.defaults.sessionStore.agentId` to the
   config in the PVC first if doctor reports unresolved legacy main rows. Skip
   this step if the instance has no legacy `sessions.json` — the spike did not.
4. Scale back up. **Health is a listener on 18789 answering HTTP, not `Running`** —
   there are no probes on this Deployment, so a crash-looping gateway reads as
   `1/1 Running`, and a gateway that starts but never binds also reads as healthy.
