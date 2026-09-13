# Public Coder TODO

Open items left over from the OpenClaw 2026.8.1 bump (#5369). The four failures
that took the agent down are resolved; these are the recurring errors and loose
ends it left behind. Diagnosis and the recovery runbook:
<../../../../openclaw/debug/2026_8_1_recovery/README.md>.

## No system agent, so two subsystems fail every minute

`agents.defaults.systemAgent.agentId` is unset, which a multi-agent config does
not infer. Two consequences, both logged once a minute:

```text
[plugins] memory-core: dreaming cron reconcile failed: Agent-less cron job has no
  resolvable owner. Pass --agent <id> when creating or editing the job, or set
  agents.defaults.systemAgent.agentId.
[config] warnings: agents.defaults.heartbeat.agentId: Multi-agent config has no
  ambient heartbeat owner; heartbeats stay disabled until
  agents.defaults.heartbeat.agentId or agents.defaults.systemAgent.agentId is set.
```

- [ ] Decide the system agent (`coder` is the Matrix-bound primary and the
      obvious candidate) and set it in `app/openclaw.json5`. Confirm afterwards
      that dreaming cron reconciles and heartbeats actually run — heartbeats have
      been off since the bump, so nothing has been exercising them.

## Matrix `auth-presence` module (resolved)

```text
[channels] failed to load persistedAuthState checker for matrix: plugin module
  path not found: .../dist-runtime/extensions/matrix/auth-presence | ENOENT
```

The warning was recorded during the 2026.8.1-era recovery, but it does not
reproduce in the current 2026.9.4 image. The bundled Matrix plugin contains
`dist/auth-presence.js` in both `dist/extensions/matrix` and
`dist-runtime/extensions/matrix`, and the healthy public-coder pod loads Matrix
without an `auth-presence` or `persistedAuthState` warning (verified 2026-09-13).

The remaining comment in `openclaw/bundle-runtime-plugin.sh` documents a separate
future-package caveat: its outward host-dependency link can fail a loader path or
alias check when `dist-runtime` is selected. It is not a missing module in the
current package, so no packaging change is needed now.

## Stale and retired config keys

Doctor reports these on every run:

- [ ] `plugins.entries.phone-control` — "plugin not found: phone-control (stale
      config entry ignored)". Remove it. It is also the only thing that would
      make a Nix-mode-off `doctor --fix` attempt a plugin auto-install.
- [ ] `mcp.apps.sandboxOrigin` unset while `gateway.auth.mode` is
      `trusted-proxy`. Widget and MCP-app frames render from the gateway port + 1
      sandbox listener, which the outpost does not route today, so those frames
      cannot load. Either route that port or set a dedicated origin.

## State PVC root ownership is not durable

`doctor --fix` failed with `EPERM: operation not permitted, fchmod` until the PVC
root was chowned to `1000:1000`; it had been left `root:root` (mode 777) by the
earlier one-off restore. That EPERM killed doctor before it reached the deferred
session-store migration, and nothing in the error named the directory.

The Deployment sets `fsGroup: 1000`, so normal provisioning should not reproduce
it — this looks like an artifact of restoring by hand.

- [ ] If this claim is ever restored manually again, chown the PVC root as part
      of the restore rather than discovering it through a doctor failure.

## Upstream bugs to file

- [ ] openclaw: `migrateHistoricalTranscriptDirectives` is not gated on
      `doctorOnlyStateMigrations`, so gateway startup runs a migration that
      depends on a doctor-only step and can only ever throw. Combined with
      `assertConfigWriteAllowedInCurrentMode` refusing `doctor --fix` whenever
      `OPENCLAW_NIX_MODE=1`, a Nix install carrying pre-2026.8.1 agent state
      cannot start and cannot repair itself.
- [ ] nix-openclaw: `stage_dist_runtime` copies `dist/extensions` into
      `dist-runtime/` without the sibling chunks those modules import, and
      OpenClaw prefers `dist-runtime/extensions` when present. Fixed downstream in
      `openclaw/gateway.nix`; upstream still ships the partial tree.
