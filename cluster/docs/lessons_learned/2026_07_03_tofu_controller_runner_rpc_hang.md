# tofu-controller Reconcile Hangs Forever When a Runner Pod Dies Mid-Init (Upstream Bug)

**Date**: 2026-07-03
**Status**: Resolved (controller restart); upstream fix filed — [flux-iac/tofu-controller#1838](https://github.com/flux-iac/tofu-controller/pull/1838) (the PR body doubles as the bug report; no separate issue)
**Affected version**: `ghcr.io/flux-iac/tofu-controller:v0.16.1` (defect identical through v0.16.4 and `main`)

## Summary

When a node running `tf-runner` pods reboots (or the pods are otherwise killed) **while
the controller is mid-reconcile**, the affected `Terraform` reconciles hang **forever**.
They freeze in `Ready=Unknown / Initializing`, stop logging entirely, and never retry or
self-heal. Each hung reconcile permanently leaks one of the controller's `--concurrent=24`
worker slots; left unchecked this progresses toward a total controller deadlock. Only a
controller restart clears it.

Trigger for this incident: **wyrm2 rebooted ~05:35 UTC**. Every `tf-runner` pod in the
cluster schedules onto wyrm2 (see "Aggravating factor"), so one reboot wedged the entire
Terraform fleet at once — including `agent-machine-access`, which provisions haku's
mailbox Authentik OIDC provider (`stalwart-haku`) and the `haku-mail-client-credentials`
secret. Presenting symptom was "haku's email automation won't reconcile."

## Root Cause

Two ingredients in the runner-RPC path combine into an unbounded hang:

1. **gRPC client uses `waitForReady: true`** — `controllers/tf_controller_runner.go:143`
   (`getRunnerConnection`'s `retryPolicy`). When the channel is in `TRANSIENT_FAILURE`
   (can't connect to a dead pod), RPCs **block waiting for the channel to become READY**
   instead of failing fast with `UNAVAILABLE`.

2. **RPC calls carry no context deadline** — every runner RPC in `setupTerraform` uses the
   raw reconcile `ctx`, e.g. `runnerClient.Init(ctx, initRequest)` at
   `controllers/tf_controller_backend.go:361`. controller-runtime imposes no per-reconcile
   timeout by default, and tofu-controller never wraps `ctx` with one. (`main.go`'s
   `RunnerCreationTimeout` bounds only _pod creation_ inside `reconcileRunnerPod`, not the
   RPCs. The 30 s `dialCtx` in `LookupOrCreateRunner` is already cancelled by
   `defer dialCancel()` before any RPC runs, and `grpc.NewClient` is lazy anyway.)

**`waitForReady:true` + a deadline-less context = an RPC to a vanished runner blocks the
reconcile goroutine forever.**

The reconcile RPC sequence in `setupTerraform` ends with `GenerateTemplate` (logs
`"generated template"`, `backend.go:346`) → **`Init`** (`backend.go:361`). When wyrm2
killed the runner pods mid-`Init`, the channel went to `TRANSIENT_FAILURE`, `waitForReady`
parked the call, and the goroutine never returned.

### Why it never self-heals

controller-runtime will not start a new `Reconcile` for a key while the previous one is
still running. A goroutine parked in `Init` means that `Terraform` object is **never
reconciled again** — hence frozen status, zero log lines, no retry. Recovering requires
killing the goroutine, i.e. **restarting the controller**. Deleting the stale runner pods
alone does _not_ help — the parked goroutines are blocked on a gRPC channel, not
re-reading pod state.

### Log signature (how to confirm)

The wedged objects' **last** controller log line is `"generated template"`, then silence.
A healthy reconcile logs either `"init reply:"` (`backend.go:380`) or `"error running
Init:"` next. Objects that got a normal Init _error_ back over a live channel (internal
DNS/DB failure inside the runner pod) recovered fine; only those whose pod **vanished
mid-`Init`** hung.

### Secondary bug: `Failed` runner pods are never cleaned up

In `reconcileRunnerPod`, a pod in `phase=Failed` (a dead runner left behind) matches none
of the state branches at `runner.go:386–401` (label present, label matches, no
`DeletionTimestamp`, phase ≠ `Running`) → `podState = stateUnknown`. The `switch` at
`runner.go:408` has **no `case stateUnknown`** → silent no-op. The dead pod is neither
deleted nor recreated, so `Error` runner pods accumulate. (Not the cause of the hang, but
why stale `*-tf-runner` pods linger in `Error`.)

## Aggravating Factor: all runners co-locate on wyrm2

Every `tf-runner` pod schedules onto **wyrm2** (observed: all runners on `10.244.5.x`).
So a single wyrm2 reboot kills the _entire_ Terraform fleet's runners simultaneously,
turning a one-node event into a cluster-wide TF outage. Worth constraining runner
scheduling (spread across OVH workers, or tolerate/agree the blast radius) as a
mitigation independent of the upstream fix.

## Compounding Factor: transient artifact-fetch drops during etcd contention

After the controller restart cleared the wedge, reconciles briefly failed with
`ArtifactFailed` instead — `tofu-controller` timing out fetching the flux source tarball
from `source-controller` (`dial ... source-controller...:80: i/o timeout`, ~8-minute
`downloadAsBytes` retry loops that never logged `generated template`). This was a **second,
transient issue that self-resolved**, not the hang.

Diagnosis (worth remembering, because the surface symptom looked like a Cilium blackhole):

- `source-controller`'s ingress is (correctly) policy-enforced: only the Flux controllers'
  Cilium identities are allowed to its artifact port `9090`. The allow-list resolves to
  `kustomize/helm/notification/image-reflector-controller` **and `tofu-controller`
  (`instance=tofu-controller`)** + its runners. A test connection from an unrelated pod
  (`litellm`) is _correctly_ dropped (`Policy denied`, `bpf_lxc.c` ingress) — that is **not**
  a bug and is easy to misread as a blackhole.
- Underlying cause: **apiserver/etcd instability** (the recurring etcd-on-HDD contention).
  `source-controller` logged `Error retrieving lease lock ... context deadline exceeded` →
  lost leader election → restarted; and Cilium identity/policy realization lagged during the
  same window, transiently dropping `tofu-controller → source-controller` fetches (worsened
  by `tofu-controller`'s pod living on freshly-rebooted wyrm2).
- Once etcd settled (`0` etcd timeouts), identities/policy realized consistently, downloads
  succeeded (`generated template` + `init reply` flowing), and the whole TF fleet reconverged
  (`NoDrift`). The exact per-packet transient could not be reproduced post-heal — an honest
  visibility boundary, but the correlation (etcd timeouts + lease losses + wyrm2 reboot) is
  solid.

Lesson: `ArtifactFailed` / ClusterIP `i/o timeout` on a Flux controller during control-plane
instability is most likely **transient Cilium identity/policy lag**, not a standing network
fault. Confirm the policy allow-list is correct and check for etcd timeouts before chasing
the datapath. Ties back to the etcd-on-HDD contention issue.

## Key Symptoms

- One or more `Terraform` CRs stuck `Ready=Unknown` / `STATUS=Initializing`, not advancing.
- `kubectl logs deploy/tofu-controller` shows **nothing** for the affected objects
  (their last line was `"generated template"`), only `"Dependencies do not meet ready
condition"` spam from dependency-blocked CRs that short-circuit before the runner phase.
- **No `tf-runner` pods `Running`** anywhere (`kubectl get pods -n flux-system | grep tf-runner`).
- Stale `*-tf-runner` pods in `Error` (`phase=Failed`, `reason=Terminated`, empty `podIP`).
- Correlates with a worker node reboot / eviction (here: `kubectl describe node wyrm2`
  Ready `LastTransitionTime`).

## Resolution

Restart the controller (clears the parked goroutines) and delete the stale runners:

```bash
kubectl -n flux-system rollout restart deploy/tofu-controller
kubectl -n flux-system rollout status  deploy/tofu-controller --timeout=90s
kubectl -n flux-system delete pod -l app.kubernetes.io/name=tf-runner \
  --field-selector status.phase=Failed
```

After restart, the controller recreates runners and reconciles normally (verified: fresh
runners `Running`, `agent-machine-access` logs `state=not-found → runner is running →
setting up terraform → plan no changes`). Dependency cascades (`haku-state`,
`forgejo-agentydragon-repos`, `haku-cloud-agent`) clear once their parents go Ready.

## Upstream Status

**Not fixed.** v0.16.1 (ours), v0.16.4, and `main` (last commit 2026-06-22) are
byte-identical on all three defects — upgrading does not help. `backend.go:52` even carries
a **commented-out** `// ctx30s, cancelCtx30s := context.WithTimeout(ctx, 30*time.Second)`:
a maintainer started adding exactly the RPC deadline that would prevent this, then
abandoned it. No open issue describes this specific failure mode (the ~52 runner/stuck
issues are about pods stuck Terminating / not created, not a goroutine parked in the `Init`
RPC).

### Why the abandoned `ctx30s` was never live

Git archaeology: the `// ctx30s` line was **born commented out** in `b98b632` "fix a secret
race condition" (2022-08-04, Chanwit Kaewkasi — tf-controller's original author). The diff
adds three pure comment lines above `UploadAndExtract`; there is **no removed live
`context.WithTimeout`**. It only reappears in `adebd78` (2022-10-08) because that refactor
moved the whole file. So it was never a working deadline that got disabled — it's an
abandoned ~4-year-old TODO, ridden in on an unrelated race-condition fix and never
followed through.

Two implications:

- The intended deadline was on **`UploadAndExtract`** (first runner RPC, ships the module
  tarball), _not_ `Init` — so even if enabled it would not have guarded the `Init` where
  this hang actually parks.
- A hardcoded 30 s is wrong in general (large module upload / slow runner cold-start can
  legitimately exceed it), which is the likely reason it was left commented rather than
  enabled. The correct fix is a **generous, configurable** deadline (or a per-reconcile
  ceiling), not a bare 30 s. This is _not_ a "they tried it and it regressed" signal — so a
  clean upstream PR is likely welcome.

Proposed upstream fix (any one breaks the hang; ideally all three):

1. Wrap runner RPCs in a bounded context (derive from `RunnerCreationTimeout`), so a dead
   channel fails instead of hanging.
2. Drop the blanket `waitForReady: true` (or scope it to genuine startup only).
3. Add `case stateUnknown` in `reconcileRunnerPod` to force-delete a `Failed` pod and
   recreate it.

### Fix we filed

[flux-iac/tofu-controller#1838](https://github.com/flux-iac/tofu-controller/pull/1838)
(branch `agentydragon:fix/runner-rpc-hang` off upstream `main`, DCO-signed, 2 commits)
implements #1 and #3:

- **`--runner-rpc-timeout`** (default `30m`) wraps the `setupTerraform` RPC batch (upload →
  backend config → init → workspace select) in a bounded context — turns "never" into
  "requeue after the timeout" and supersedes the abandoned `ctx30s` TODO. `plan`/`apply`
  are left unbounded (they can legitimately run long); a larger budget for them is noted as
  follow-up.
- **`case stateUnknown`** in `reconcileRunnerPod` force-deletes a `Failed` pod and recreates
  it.

Validated locally with `go build` + `go vet`; the envtest integration suite is CI-run (it
fails identically on unmodified `main` in a local sandbox, so it's a harness/env issue, not
the change). This is a low-traffic upstream — expect review latency; track the PR before
assuming it lands.

## Key Lessons

1. **A controller restart is the only recovery** — the hang is a parked goroutine, not
   stale cache or a stuck pod; deleting pods alone won't unwedge it.
2. **`waitForReady:true` + deadline-less context is an infinite-hang footgun** — any gRPC
   client that waits-for-ready must be paired with a per-call deadline.
3. **This recurs on every node reboot / runner eviction that lands mid-reconcile**, and
   each occurrence permanently leaks a worker slot. Watch `tf-runner` pods and TF CR
   status after any wyrm2 reboot.
4. **Runner co-location on wyrm2 is the blast-radius multiplier** — one node reboot = whole
   TF fleet down. Distinct from this upstream bug; fixable in our config.
5. **A `Policy denied` drop from an _unrelated_ pod is correct enforcement, not a fault** —
   when chasing a "blackhole," verify the source identity is one that _should_ be allowed
   before concluding the datapath is broken. `ArtifactFailed` / ClusterIP `i/o timeout` on a
   Flux controller during control-plane instability is most likely transient Cilium
   identity/policy lag (check for etcd timeouts first), not a standing network fault.
6. Distinct from the 2025-11-19 TLS-cache-desync bug
   (<2025_11_19_tofu_controller_tls_cache_desync.md>), which also resolves via restart but
   has a different root cause (startup GC vs. in-memory cache).
