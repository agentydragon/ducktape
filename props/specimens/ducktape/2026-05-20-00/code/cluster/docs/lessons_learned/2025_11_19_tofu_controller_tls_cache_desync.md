# tofu-controller TLS Secret Cache Desync (Upstream Bug)

**Date**: 2025-11-19
**Status**: Resolved (workaround; upstream bug not fixed)

## Root Cause

Bug in tf-controller's startup garbage collection (`mtls/rotator.go`):

1. Controller starts, sets `referenceTime = time.Now()` (line 164)
2. Startup GC deletes all secrets where `CreationTimestamp.Before(referenceTime)` — which
   is ALL existing TLS secrets (line 325)
3. In-memory cache (`knownNamespaceTLSMap`) is NOT cleared
4. New reconciliation requests hit cache: "TLS already generated" (line 264)
5. Runner pod starts, references deleted secret, crashes: "secrets not found"

The bug is that `time.Now()` as reference point deletes every pre-existing secret (they
were all created before "now"), but the cache still thinks they exist.

**Code location**: `github.com/weaveworks/tf-controller/mtls/rotator.go`

## Key Symptoms

- Terraform runner pods in CrashLoopBackOff: `secrets "terraform-runner.tls-XXXXXXXX" not found`
- Controller logs: `"TLS already generated for"` (cache hit, but secret is gone)
- `kubectl get secret -n flux-system -l app.kubernetes.io/name=tf-runner` returns nothing
- All Terraform resources stuck in "Reconciliation in progress"

## Resolution

Restart tofu-controller to clear in-memory cache and regenerate TLS secrets:

```bash
kubectl rollout restart deployment/tofu-controller-tf-controller -n flux-system
kubectl wait --for=condition=available --timeout=60s deployment/tofu-controller-tf-controller -n flux-system
kubectl delete pods -n flux-system -l app.kubernetes.io/name=tf-runner
```

If that doesn't work, suspend/resume all Terraform resources:

```bash
kubectl get terraform -n flux-system -o name | xargs -I {} kubectl patch {} -p '{"spec":{"suspend":true}}' --type=merge
kubectl delete pods -n flux-system -l app.kubernetes.io/name=tf-runner
kubectl rollout restart deployment/tofu-controller-tf-controller -n flux-system
kubectl wait --for=condition=available --timeout=60s deployment/tofu-controller-tf-controller -n flux-system
kubectl get terraform -n flux-system -o name | xargs -I {} kubectl patch {} -p '{"spec":{"suspend":false}}' --type=merge
```

## Key Lessons

1. **Controller restart clears in-memory caches** — when TLS secrets disappear but the
   controller thinks they exist, a restart forces regeneration.
2. **Startup GC with `time.Now()` is a bug** — all pre-existing secrets are "before now".
   The proposed fix: use `time.Now().Add(-cr.CAValidityDuration)` to only delete genuinely
   expired secrets.
3. **Monitor runner pods after controller restarts** — the bug triggers on every controller
   restart, not just first boot.
