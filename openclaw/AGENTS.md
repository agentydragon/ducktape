@README.md

## Version bumps

A bump is not just the version string: **2026.8.1 took the public-coder agent down
for ~5 hours across four independent failures**, three of which shared one shape —
startup detects a pending state migration and hard-fails, only `doctor` performs
it, and `doctor` refuses to run under Nix. Full diagnosis and the recovery
runbook: <debug/2026_8_1_recovery/README.md>.

Before bumping, read the upstream release notes for **state migrations and
retired config keys**, and expect a maintenance window for any instance whose
state predates the release.

- **Both images move together.** They share `gateway.nix`, so a bump lands in
  public-coder and the Haku spike at once. Build both before merging; each has its
  own image workflow and its own deployment to recover if the bump goes wrong.
- **An instance cannot repair itself.** `assertConfigWriteAllowedInCurrentMode`
  aborts `doctor --fix` whenever `OPENCLAW_NIX_MODE=1`, which the wrapper sets. If
  a release adds a state migration, the gateway refuses to start until it runs and
  the only tool that runs it refuses to start too. Recovery is a one-off Job with
  `OPENCLAW_NIX_MODE=0` — and `""` will not do, because `--set-default` expands to
  `${OPENCLAW_NIX_MODE:-1}`.
- **Retired config keys are ignored silently.** They keep reading as if they still
  apply while the behaviour they suppressed comes back — this is how the Control UI
  started demanding device pairing. Run `openclaw doctor` after a bump and act on
  "Legacy config keys detected".
- **`postInstall` does not run.** nix-openclaw supplies a complete custom
  `installPhase` and never calls `runHook postInstall`, so a hook added there is
  skipped in silence: the build succeeds having done nothing. Append to
  `installPhase`. A fail-closed check placed in `postInstall` passes vacuously,
  which is worse than no check.
- **Verify the built artifact, not the expression.** The `dist-runtime` staging
  bug and the `stage_acpx` edge case were both invisible until the derivation was
  actually built and its output inspected; local simulation of the upstream tarball
  missed both, because the real tree has content the tarball does not.

## Verifying a rollout

There are no probes on these Deployments, so `1/1 Running` means only that a
process exists. A crash-looping gateway and one that starts but never binds both
read as healthy. Confirm a listener on the gateway port and an HTTP response:

```bash
kubectl -n <ns> exec <pod> -c openclaw -- sh -c \
  'cat /proc/net/tcp /proc/net/tcp6 | grep -c 4965'   # 18789 == 0x4965
```

An HTTP 403 from inside the pod is the gateway's own auth response and means it is
serving; `000` means nothing is listening.

**Manually suspending a Flux Kustomization does not hold.** These Kustomization
objects are themselves reconciled by `flux-system`, which clears `spec.suspend`
and restores `replicas: 1` within a reconcile interval. Scale down and work
promptly rather than relying on a suspend.
