# Reviving OpenShell for an experiment

OpenShell was deleted from the cluster on 2026-07-31 (#3607) along with the
OpenClaw gateway that was its only consumer. This is what you need to bring it
back, and — more importantly — what to do differently.

**Do not reinstate the global `openshell-system` install.** That shape is what
made the teardown necessary: a cluster-wide stateful gateway, owned by Flux,
serving exactly one agent, left running and wedged for days because nothing
depended on it noticing. Scope the revival to the experiment that needs it and
delete it with the experiment. The decision record is
[verdicts.md](verdicts.md) § Isolation and sandboxing.

## What it was

| Piece      | Detail                                                                                                                                                                         |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Chart      | `openshell-operator` **0.4.0** from `oci://ghcr.io/lensapp/charts` (Lens-authored, wrapping NVIDIA's OpenShell and delegating runtime pods to `kubernetes-sigs/agent-sandbox`) |
| Gateway    | bundles OpenShell gateway **0.0.90**, a **stateful** StatefulSet with a SQLite DB                                                                                              |
| CRDs       | `openshell{sandboxes,policies,providers,providerprofiles,workspaces}.openshell.lenshq.io`, installed `CreateReplace`                                                           |
| Namespaces | `openshell-system` (gateway + operator), `openshell-sandboxes` (runtime pods)                                                                                                  |

NVIDIA also publishes its own chart (`oci://ghcr.io/nvidia/openshell/helm-chart`)
independent of the Lens one. The cluster used Lens's.

Everything is recoverable from git: `git show 63aa2a1d7^:cluster/k8s/agents/openshell/<path>`.

## Five things that will bite you again

These cost days the first time. None is obvious from upstream docs.

1. **The sandbox namespace needs privileged Pod Security.** OpenShell's
   supervisor requires an Unconfined AppArmor profile plus `SYS_ADMIN`,
   `NET_ADMIN`, `SYS_PTRACE` and `SYSLOG`. The cluster default is `baseline`,
   which rejects every generated sandbox pod _before scheduling_ — so the
   failure looks like nothing happening rather than an error.
2. **Own the sandbox policy explicitly.** The community image copies its own
   `/etc/openshell/policy.yaml` into the gateway when `openshell sandbox create`
   gets no `--policy`, and the mutable `openclaw:latest` tag has historically
   granted unrelated Claude, GitLab and NVIDIA egress that way. Package your own
   policy into a ConfigMap and pass its path; keep the security boundary
   independent of image contents.
3. **Do not chain it behind the shared mitmproxy.** `openshell-sandboxes` was
   deliberately excluded from the Kyverno mitmproxy injection and the
   force-proxy Cilium policy. OpenShell's supervisor proxy needs to see and
   authorize the original HTTPS requests; a second intercepting proxy in front
   of it breaks credential substitution.
4. **The chart does not expose `storageClassName`** for the gateway's
   StatefulSet claim, and this cluster has no default StorageClass. The old
   install needed a Flux `postRenderer` for the gateway plus a Kyverno
   `ClusterPolicy` defaulting the claim templates on OpenShell-managed
   `Sandbox` objects.
5. **The gateway is stateful and its schema migrates forward only.** 0.0.90
   applied a workspace migration that 0.0.86 cannot read. Do not downgrade a
   gateway that has already run.

If you pair it with OpenClaw again, you also need the CLI name-length shim
(`openshell-cli-compat`, mounted over the plugin's `command`), which tracks
`openclaw/openclaw#114177` — a draft blocked because the new naming scheme has
no migration for existing sandboxes.

## Why the global install was the wrong shape

Measured, not assumed:

- **F1** — a second supervisor invocation permanently breaks the sandbox's SSH
  relay. `kubectl exec` into a sandbox pod reproduces it exactly and
  irreversibly, which is why that is a standing prohibition.
- **F2** — egress policy is enforced **per process**, not per pod. A process
  launched outside the supervisor bypasses the policy entirely, so OpenShell is
  not a substitute for a NetworkPolicy.
- **F11** — the whole harness cannot run under OpenShell on the k8s operator.
- **F13** — OpenClaw _does_ run inside an OpenShell sandbox on the Docker
  driver, so the idea is sound; the k8s operator path is where it fails.
- **The wedge trigger was never identified.** Production wedged on 2026-07-28
  with nobody exec'ing into it. Ruled out since: the `process` tool alone,
  ordinary `exec` use, six abandoned yielding background sessions, and a
  `gh`/`git` probe. Until that is known, an OpenShell-backed agent cannot be
  trusted unattended — which is the single strongest argument for scoping any
  revival to a supervised experiment with a deletion date.

## The shape to revive into

Put the gateway, its namespace, its policy and its sandboxes under one
experiment-scoped kustomization with an explicit expiry, the way `agent-lab`
was run — a `CLEANUP` tombstone naming the date and a revert PR prepared
alongside it, so the teardown is written before the experiment starts rather
than reconstructed months later from a wedged deployment.

Prefer `k3d` first. Most OpenShell questions that mattered here — does the
plugin work, does the policy apply, does the harness come up — are answerable
on a local cluster without production RBAC, and only the questions that
genuinely need cluster identity or cluster-only services justify the real one.

## Leftovers from the teardown

`cluster/docs/troubleshooting.md` § "Removing a CRD Operator (Uninstall Runbook)"
already covers this ground — Helm not removing CRDs, finalizers stalling once the
controller is gone, the clear-the-finalizer patch, and a final verification
checklist. #3607 did not follow it, and paid for all three: `kubectl delete ns
openshell-system` hung in `Terminating` on an `OpenShellProvider` whose
`openshell.lenshq.io/provider-cleanup` finalizer had no controller left, and the
five CRDs outlived the uninstall.

Read the runbook rather than this section. It had one gap, which is why I read
it as not applying: it attributed the stall to deleting controller pods by hand,
and a pure GitOps removal hits it just as reliably when the operator and its
custom resources are pruned in the same commit — the shape this project used.
That correction and the later `openclaw-gateway` cleanup shipped separately as
operational changes.

For the record, what unstuck it:

```bash
kubectl -n openshell-system patch openshellprovider agentydragon-github \
  --type=merge -p '{"metadata":{"finalizers":[]}}'
```

Safe **only because** the controller was gone and its cleanup — deregistering a
provider from a gateway that no longer exists — had become a no-op. Never reach
for the namespace's own `spec.finalizers` via the `/finalize` subresource; that
deletes the namespace object while orphaning its contents in etcd.

The same cleanup was later required for `openclaw-gateway`: a stuck
`OpenClawInstance`, the StatefulSet it owned, 21 GiB of PVCs, and three
`openclaw.rocks` CRDs. The completed teardown is recorded in
<../../cluster/archive/2026_08_openclaw_namespace_retirement.md>.
