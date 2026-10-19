@README.md

## Version bumps

Read upstream release notes for **state migrations and retired config keys**; expect a
maintenance window for any instance whose state predates the release. 2026.8.1 took
public-coder down ~5h; diagnosis and recovery runbook: <debug/2026_8_1_recovery/README.md>.

- **An instance cannot repair itself.** Startup hard-fails on a pending state migration; only
  `doctor --fix` runs it, and `assertConfigWriteAllowedInCurrentMode` aborts it under
  `OPENCLAW_NIX_MODE=1`, which the wrapper sets. Recover with a one-off Job setting
  `OPENCLAW_NIX_MODE=0` (`""` does not work: `--set-default` expands `${OPENCLAW_NIX_MODE:-1}`).
- **Retired config keys are ignored silently** and the behaviour they suppressed returns (how
  the Control UI started demanding device pairing). Run `openclaw doctor` after a bump and act
  on "Legacy config keys detected".
- **`postInstall` never runs**: nix-openclaw's custom `installPhase` skips
  `runHook postInstall`, so hooks (and fail-closed checks) there pass vacuously. Append to
  `installPhase`.
- **Dist patches fail closed by design.** <patches/openclaw-2026.9.5-dist.patch> matches exact
  upstream context; when a release moves a line, re-find the construct and update the
  release-specific patch. Never loosen a match — the behaviour silently reverts to upstream.
  Each hunk is a downstream Nix/runtime contract; retire one only when upstream exposes
  equivalent behaviour and the image passes the affected Nix-mode, plugin and startup checks.
- **Verify the built derivation's output**, not the expression or a simulated tarball (the
  `stage_acpx` edge case only showed in the real build). nix-openclaw now makes
  `dist-runtime` a symlink to `dist`; add no copy/link workaround unless upstream regresses
  that.

## Verifying a rollout

The Deployments have no probes, so `1/1 Running` proves nothing. Check for a listener on the
gateway port (18789 = `0x4965`):

```bash
kubectl -n <ns> exec <pod> -c openclaw -- sh -c 'cat /proc/net/tcp /proc/net/tcp6 | grep -c 4965'
```

HTTP 403 from inside the pod is the gateway's auth (serving); `000` means nothing listens.

Suspending these Flux Kustomizations by hand does not hold: `flux-system` reconciles them,
clearing `spec.suspend` and restoring `replicas: 1`. Scale down and work promptly.
