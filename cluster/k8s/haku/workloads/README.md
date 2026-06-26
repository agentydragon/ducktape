# haku/workloads — Flux pipe for Haku's self-authored workloads

Operator-owned plumbing that lets Haku run **persistent workloads** in `haku-sandbox`
via GitOps (not just ad-hoc `kubectl apply`), while keeping the perimeter operator-owned.
Haku writes manifests under `k8s/` in its `haku-state` repo (seeded from
`haku/state_template/k8s/`); this pipe reconciles them.

| Object                         | Kind                 | Namespace      | Role                                                                         |
| ------------------------------ | -------------------- | -------------- | ---------------------------------------------------------------------------- |
| `haku-state`                   | `GitRepository`      | `flux-system`  | source: Haku's state repo (internal Forgejo, read-only pull)                 |
| `haku-state-workloads`         | `Kustomization`      | `flux-system`  | reconciles `haku-state` `./k8s` → `haku-sandbox`, impersonating the SA below |
| `haku-state-reconciler`        | `ServiceAccount`     | `flux-system`  | impersonation identity for the apply                                         |
| `haku-state-workload-deployer` | `Role`/`RoleBinding` | `haku-sandbox` | the only grant the SA has                                                    |

**Containment.** The apply runs as `haku-state-reconciler` (`serviceAccountName` on the
Kustomization), so it can only do what its Role allows: Deployments/StatefulSets/
DaemonSets/ReplicaSets, Services/ConfigMaps/PVCs, Jobs/CronJobs — **no Secrets, no
Gateway-API routes** (Kyverno denies routes in `haku-sandbox` too). `targetNamespace:
haku-sandbox` forces everything into the one namespace regardless of what the manifests
declare. So Haku gets a GitOps workload path that can never widen its own perimeter — it
applies a strict subset of what Haku itself already could.

**Trust posture (flagged for review).** This points cluster Flux at a repo Haku can
write. The kustomize-controller will build agent-authored kustomize; the impersonation
SA + `targetNamespace` pin + `prune` + the Kyverno route-deny are what bound the blast
radius. Basic-auth uses the existing `haku-state-git-write` Secret (the only creds; the
`haku` Forgejo user is r/w on its own repo — there is no separate read principal). A
read-only deploy key would be tighter; deferred.

Until Haku first seeds `k8s/`, `haku-state-workloads` is `NotReady` (path not found) —
expected pre-first-run. First workload: the `haku-ui` placeholder
(`haku/console/plans/free_form_ui_iframe.md`).
