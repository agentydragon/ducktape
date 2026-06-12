# Longhorn Uninstall + Hidden Gitops Wedge

**Date**: 2026-05-13
**Status**: Resolved

## Symptoms

- `flux-system/study-casino`, `docker-ci`, `grocy-{sf,vallejo}`, `cpap-sync`, `tana-mcp`,
  `tofu-state-backup` all stuck `Ready=False` with the same error on PVC dry-run:

  ```text
  failed calling webhook "validator.longhorn.io": failed to call webhook:
  Post "https://longhorn-admission-webhook.longhorn-system.svc:9502/v1/webhook/validation?timeout=10s":
  dial tcp 10.105.249.195:9502: connect: operation not permitted
  ```

- "We suspended longhorn already" — but `kubectl -n flux-system get kustomization longhorn -o jsonpath='{.spec.suspend}'` returned empty.
- The longhorn admission-webhook Deployment was already gone; the
  `ValidatingWebhookConfiguration` / `MutatingWebhookConfiguration` were still present
  with `failurePolicy: Fail`, matching `""/persistentvolumeclaims` and `""/nodes`
  cluster-wide — every PVC create across the cluster was getting rejected.

## Root Cause

**Two stacked issues, the outer one masked the inner.**

1. **Root `flux-system` Kustomization was wedged.** Commit
   `91dc9ba34 "Bump Go deps... wire github-repo-rulesets flux kustomization"`
   added `cluster/k8s/github-repo-rulesets/flux-kustomization.yaml` to the root
   `cluster/k8s/kustomization.yaml`. But that directory had been reverted twice
   already (`cc83f8dec`, `e3e010068`) and never re-tracked — it lived only as
   untracked files in someone's local working copy. From Flux's perspective the
   reference was dangling, so the root Kustomization failed `kustomize build`:

   ```text
   accumulating resources from 'github-repo-rulesets/flux-kustomization.yaml':
   open ...: no such file or directory
   ```

   Once that failed, **every change committed to `devel` thereafter sat in git
   without being applied** — including the longhorn-suspend commit `7500b528e`
   and several other memory-pressure suspends.

2. **Partial longhorn manual cleanup** left zombie webhook configs. The intent
   was "suspend longhorn and delete its cluster footprint", but only some
   resources were deleted by hand (the webhook backing Deployment among them).
   `helm uninstall` was never run, so the webhook _configs_ (cluster-scoped)
   stayed registered while the _backing pods_ were gone. With
   `failurePolicy: Fail`, every PVC/Node admission call dialed the
   now-unreachable Service and failed open as `operation not permitted`.

## Solution

### Sequence that actually worked

1. **Drop the dangling reference** from the root kustomization (commit `ad218f77f`,
   also deleted the untracked `cluster/k8s/github-repo-rulesets/` working-copy
   files so the precommit validator passed).
2. After Flux reconciled, the in-cluster `longhorn` Kustomization picked up
   `spec.suspend: true` from `7500b528e`.
3. Patched both webhooks to `failurePolicy: Ignore` — restored cluster-wide
   PVC/Node admission immediately while uninstall ran.
4. Patched the `longhorn` HelmRelease `spec.suspend: true` (so the
   helm-controller wouldn't fight the uninstall).
5. Set `kubectl -n longhorn-system patch settings.longhorn.io deleting-confirmation-flag --type=merge -p '{"value":"true"}'`
   — Longhorn requires this gate set on its `settings` CR before its uninstall
   job will run, otherwise the job fast-fails with
   `cannot uninstall Longhorn because deleting-confirmation-flag is set to false`.
6. `helm uninstall longhorn -n longhorn-system --wait`. The chart's own
   uninstall hook (the `longhorn-uninstall` Job) runs `UninstallController`
   which deletes the Longhorn CRs in order.
7. The uninstall Job got stuck looping `Found 1 backuptargets remaining`. The
   longhorn-manager that processes the `longhorn.io` finalizer was already gone
   (manual deletion earlier), so the finalizer would never come off. Cleared
   by hand:
   ```bash
   kubectl -n longhorn-system patch backuptarget.longhorn.io default \
     --type=merge -p '{"metadata":{"finalizers":[]}}'
   ```
   Then the uninstall job completed and Helm uninstall returned 0.
8. Final residue cleanup (Helm chart does _not_ clean these by default):
   ```bash
   kubectl get crd -o name | grep '\.longhorn\.io$' | xargs kubectl delete
   kubectl delete validatingwebhookconfiguration longhorn-webhook-validator
   kubectl delete mutatingwebhookconfiguration   longhorn-webhook-mutator
   kubectl delete storageclass longhorn longhorn-static hetzner-longhorn hetzner-longhorn-rwx
   kubectl delete ns longhorn-system
   ```

## Future Process: Suspend / Uninstall Runbook

Promoted to <../troubleshooting.md> § "Removing a CRD Operator (Uninstall
Runbook)" — the generalized procedure lives there now.

## References

- Unwedge commit: `ad218f77f` ("cluster: drop dangling github-repo-rulesets reference from root kustomization")
- Original longhorn-suspend commit (delayed by the wedge): `7500b528e`
- Reverts that left `github-repo-rulesets/` untracked: `cc83f8dec`, `e3e010068`
- Commit that introduced the dangling reference: `91dc9ba34`
