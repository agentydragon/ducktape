# Public Coder TODO

Open items left over from the OpenClaw 2026.8.1 bump (#5369). The four failures
that took the agent down are resolved; these are the recurring errors and loose
ends it left behind. Diagnosis and the recovery runbook:
<../../../../openclaw/debug/2026_8_1_recovery/README.md>.

## Released PV from the retired state claim

`public-coder-agent-state` (the pre-`state-v2` claim) is gone from GitOps, but its
volume is not. local-path placed it on **ovh-ns103656, a control-plane node** the
gateway is pinned away from — which is why it had to be replaced rather than
reused, and why nothing has read it since `state-v2` landed.

Its reclaim policy was flipped `Delete` → `Retain` before the prune, so Flux left
the PV `Released` and the directory intact instead of destroying it. That was
deliberate: nothing backs this claim up (volsync covers `state-v2` only) and its
contents were never confirmed empty. Until both the PV and the directory are
gone, this is a `Released` PV holding ~10Gi of control-plane disk for nothing.

- [ ] Confirm nothing is wanted from the directory below on ovh-ns103656, then
      delete both:

```bash
# on ovh-ns103656 (Talos: no shell — mount the PV in a Pod tolerating the
# control-plane taint, or use a privileged debug container)
/var/mnt/local-path-ovh-hdd/local-path/pvc-29a423f0-42e9-4ea1-9b4d-61fa7e725ec1_public-coder-agent_public-coder-agent-state

kubectl delete pv pvc-29a423f0-42e9-4ea1-9b4d-61fa7e725ec1
```

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

## Matrix `auth-presence` module missing from `dist-runtime`

```text
[channels] failed to load persistedAuthState checker for matrix: plugin module
  path not found: .../dist-runtime/extensions/matrix/auth-presence | ENOENT
```

`openclaw/gateway.nix` stages the shared chunks into `dist-runtime/`, but
`bundle-runtime-plugin.sh` adds the Matrix plugin to the extension trees _after_
that fixup runs, so this path is outside what the fixup repairs and outside what
its fail-closed guard checks. Matrix itself works — it loads from
`dist/extensions/matrix/dist/index.js` — so only the persisted-auth-state checker
is degraded.

- [ ] Establish whether this predates 2026.8.1 or is new, then either extend the
      staging to cover bundled runtime plugins or fix the plugin layout. Whatever
      the fix, extend the guard to cover it: it passes today only because it runs
      before the plugin exists.

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
