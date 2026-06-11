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

The lesson: "suspend the Flux resource + delete some pods by hand" is **not**
equivalent to an uninstall. Cluster-scoped admission webhooks make the order
matter, and Helm/chart-specific uninstall hooks exist for a reason.

### Standard order for removing a CRD operator

1. **Drain consumers first**
   - PVCs on the operator's StorageClass: migrate or accept loss explicitly.
   - All CRs of the operator's CRDs: delete or migrate.
   - Anything that imports the operator's webhook: only matters if `failurePolicy: Fail`.

2. **Pre-relax `failurePolicy` to `Ignore` on every admission webhook the operator owns.**
   Do this _before_ the operator pods come down so unrelated workloads keep admitting:

   ```bash
   kubectl patch {validating,mutating}webhookconfiguration <name> --type=json \
     -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Ignore"}]'
   ```

3. **Remove via gitops, not via `kubectl delete`**. For Flux HelmReleases:
   delete the `HelmRelease` manifest from git, let Flux's prune run
   `helm uninstall`. Helm uninstall runs the chart's own teardown hooks (job
   ordering, CR deletion, ClusterRole/Service cleanup). Ad-hoc
   `kubectl delete deploy` leaves all of that behind.

4. **Set chart-specific "really uninstall" gates before pruning.** Some
   operators block uninstall until a CR is patched:
   - Longhorn: `settings.longhorn.io/deleting-confirmation-flag: "true"`
   - Cloudnative-PG: `prune-confirmed` annotation on clusters
   - Strimzi: explicit `pause-reconciliation` removal

5. **Helm uninstall hooks may stall on finalizers** if you've already deleted
   the controller pods by hand. Identify and clear them:

   ```bash
   kubectl -n <ns> get <crd> -o jsonpath='{range .items[*]}{.metadata.name}: {.metadata.finalizers}{"\n"}{end}'
   kubectl -n <ns> patch <crd> <name> --type=merge -p '{"metadata":{"finalizers":[]}}'
   ```

6. **CRDs and webhook configs are NOT cleaned by Helm uninstall by default.**
   Explicitly delete after `helm uninstall` completes:

   ```bash
   kubectl get crd -o name | grep '<operator-domain>$' | xargs kubectl delete
   kubectl delete {validating,mutating}webhookconfiguration <name>
   ```

7. **Final verification, every uninstall, every time:**
   ```bash
   kubectl get crd | grep <op>
   kubectl get {validating,mutating}webhookconfiguration | grep <op>
   kubectl get clusterrole,clusterrolebinding | grep <op>
   kubectl get pv,pvc -A -o wide | grep <op-storageclass>
   kubectl get ns <op-ns>
   ```

### Emergency unblock (webhook is already wedging the cluster)

If you arrive _after_ the zombie webhook is already rejecting unrelated PVCs:

```bash
kubectl patch validatingwebhookconfiguration <name> --type=json \
  -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Ignore"}]'
kubectl patch mutatingwebhookconfiguration   <name> --type=json \
  -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Ignore"}]'
```

This unblocks the cluster within seconds. Then proceed with the slow/correct
uninstall above.

### Sanity-check the gitops root when nothing propagates

When suspends/edits committed to `devel` don't reach the cluster, suspect a
wedged top-level Kustomization. A failed `kustomize build` at the root
silently freezes every downstream Kustomization:

```bash
kubectl -n flux-system get kustomization flux-system -o jsonpath='{.status.conditions[*].message}{"\n"}'
```

A `kustomize build failed: ... no such file or directory` message there means
some `cluster/k8s/.../flux-kustomization.yaml` reference in
`cluster/k8s/kustomization.yaml` points at a path that doesn't exist in the
tracked tree. Fix the reference (or recommit the missing directory), then
all the queued-up downstream changes apply at once.

A pre-commit `cluster-validate` check that catches dangling resource
references in `cluster/k8s/kustomization.yaml` would prevent this class of
silent wedge entirely.

## References

- Unwedge commit: `ad218f77f` ("cluster: drop dangling github-repo-rulesets reference from root kustomization")
- Original longhorn-suspend commit (delayed by the wedge): `7500b528e`
- Reverts that left `github-repo-rulesets/` untracked: `cc83f8dec`, `e3e010068`
- Commit that introduced the dangling reference: `91dc9ba34`
