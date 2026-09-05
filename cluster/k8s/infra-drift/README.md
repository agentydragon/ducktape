# infra-drift — plan-only watch on the metal root

`cluster/terraform/main` is applied from a workstation by
`bazel run //cluster:bootstrap`. Its state lives in the in-cluster CNPG
`tofu-state-db-ovh` (schema `main`), so a workstation that is not a k8s worker
needs a `kubectl port-forward` before it can even plan — and nothing plans it
between applies.

This `Terraform` CR closes that gap from the side where the state DB is local:
tofu-controller plans the root on an interval and reports the diff. It never
applies. `planOnly: true` is the whole contract — there is deliberately no
`approvePlan`, because approving is not a thing this CR should be able to do.
Applying stays `bazel run //cluster:bootstrap`.

## What this covers

`spec.targets` scopes the plan to `ovh_dedicated_server.{kimsufi,kimsufi_cp}` —
the OVH bare-metal server resources. Their managed attributes are exactly the
kind that get changed out of band: `rescue_ssh_key`, `display_name`, and
`efi_bootloader_path`, whose comment in `ovh-nodes.tf` explains that losing it
sends the node into an rEFInd boot loop. `kimsufi_cp` currently resolves to
zero instances (`local.kimsufi_cp_servers` is empty) and is listed so a future
Kimsufi control plane is covered without editing the CR.

That is the one subgraph in this root whose whole dependency closure is the
`ovh` provider plus `secrets/ovh-{credentials,rescue-ssh}.sops.yaml`.
Everything else is excluded:

| Excluded                                                              | Why                                                                                                                                                                                                                                     |
| --------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `proxmox_*` (VMs, persistent role/user/token)                         | Deferred — the provider authenticates as `root@pam!tofu`, a full-root API token, which is a much larger thing to put in a runner pod than scoped OVH credentials. `cluster/k8s/TODO.md` § Proxmox drift watch.                          |
| `ovh_dedicated_server_update.*`, `ovh_dedicated_server_reboot_task.*` | The provisioning chain. `-target` pulls in dependencies, and `_harddisk` `depends_on` the rescue-mode `dd` step, which reaches back through both reboot tasks — so targeting the steady-state boot mode drags the one-shots in with it. |
| `local_file.{kubeconfig,talosconfig}`                                 | Written into the module dir on the operator's workstation. A fresh pod checkout does not have them, so every plan would report them as missing.                                                                                         |
| `kubernetes_*`, `helm_*`                                              | Both providers are configured with `config_path = "${path.module}/kubeconfig"` — the file above.                                                                                                                                        |
| `null_resource.*` bootstrap steps                                     | `local-exec` against `kubectl`, `helm` and `cilium` with that same kubeconfig.                                                                                                                                                          |
| `talos_*`                                                             | Machine config carries the Nebula node identities, whose private keys are per-host SOPS files.                                                                                                                                          |
| `data.sops_file.cluster_secrets_age`                                  | Reads `secrets/shared/cluster-secrets-age.yaml`, which is not encrypted to the cluster key the runner carries — and its `kubernetes_secret` consumer needs the kubeconfig above anyway.                                                 |

Targeting is not just a scoping preference here: `cluster/k8s/TODO.md` records
that an untargeted `tofu plan` on this root still stalls during provider
refresh.

## Enabling

The Flux Kustomization ships `suspend: true`. One step remains, and it needs
SOPS decryption:

1. **Re-encrypt the two OVH files to the cluster key.** `.sops.yaml` already
   lists `*cluster-secrets` as a recipient of
   `secrets/ovh-{credentials,rescue-ssh}.sops.yaml`; the ciphertext has not
   caught up:

   ```bash
   sops updatekeys secrets/ovh-credentials.sops.yaml
   sops updatekeys secrets/ovh-rescue-ssh.sops.yaml
   ```

2. Drop `suspend: true` from `flux-kustomization.yaml` and commit both.

No new Secret to plant: the runner reads `SOPS_AGE_KEY` from the existing
`flux-system/sops-age-cluster-secrets`, the same identity Flux decrypts
manifests with.

### Why the broad key rather than a narrow one

`tf-runner-role` is a `ClusterRole`, bound cluster-wide, granting
`get/list/watch/create/update/patch/delete` on `secrets` in **every** namespace.
Every tf-runner pod can therefore already read `sops-age-cluster-secrets`
itself. A single-purpose age key would have been stored as a cluster Secret
too, reachable by exactly the same principals — ceremony, not containment.

The one real consequence: these two files become decryptable by anything
holding the cluster key, which includes the `wyrm2-host` identity via
`secrets/shared/cluster-secrets-age.yaml`. Before, they were reachable only
from the five user keys. That widening is small but not nil; it is the price of
not maintaining a key whose only protection was against an attacker who could
already bypass it.

## Reading a plan

The Kustomization's `healthChecks` entry tracks the CR's `Ready` condition, so
a plan that finds changes surfaces as a NotReady `infra-drift` Kustomization —
the same place `flux get kustomizations` and the `cluster_health` scan already
look. That is the notification; the ConfigMap below is the detail.

`storeReadablePlan: human` puts the diff in a ConfigMap next to the CR:

```bash
kubectl -n flux-system get terraform infra-drift
kubectl -n flux-system get cm | grep tfplan
kubectl -n flux-system get cm <name> -o jsonpath='{.data.tfplan}'
```

`.status.lastDriftDetectedAt` and `.status.lastPlanAt` carry the timestamps.

## State lock contention

A `tofu plan` takes the PG advisory lock on schema `main` — the same lock
`bazel run //cluster:bootstrap` needs. `interval: 6h` keeps the overlap small;
a shorter interval trades operator interruptions for freshness.

Two failure modes to expect:

- **Bootstrap hits a held lock.** Wait for the plan to finish, or
  `flux -n ducktape-flux suspend kustomization infra-drift` followed by
  `kubectl -n flux-system patch terraform infra-drift --type=merge -p '{"spec":{"suspend":true}}'`
  for the duration of the work — patching the CR alone is reverted at the next
  Kustomization sync (10m).
- **Orphaned lock after a runner dies.** A killed runner pod can leave a
  session-scoped advisory lock held by an idle PostgreSQL backend, which then
  blocks bootstrap until that backend is terminated. It happened to
  `sso-providers` after a wyrm2 reboot; the recovery query is in
  <../../docs/lessons_learned/2026_07_11_tofu_pg_orphaned_session_lock.md>.
  Blocking a `tf/gitops` root delays a reconcile — blocking this one stops
  cluster bootstrap, which is the reason the interval is hours and not minutes.

## Source: why a dedicated GitRepository

The shared `flux-system` GitRepository sparse-checks-out only deployment paths:

```text
sparseCheckout: [cluster/k8s/, loom/wayback/deploy/, props/deploy/, tf/gitops/]
```

`cluster/terraform/` is not among them, so the runner's checkout has no such
directory and the plan dies with
`terraform path not found: stat …/cluster/terraform/main`. That list cannot be
extended: `cluster/k8s/flux-system/gotk-sync.yaml` is flux-generated and marked
DO NOT EDIT. Hence `infra-drift-source`, scoped to what this plan reads and
nothing else, which also keeps the shared artifact every other Flux consumer
pulls from growing.

It uses `ignore`, not `sparseCheckout`, because `sparseCheckout` takes
directories and this root reads a repo-root **file** —
`jsondecode(file("${path.module}/../../../nebula-mesh.json"))` in `nebula.tf`.
Two gotchas are baked into that rule list:

- **`-target` does not prune `locals`.** The nebula locals are evaluated on
  every plan even though nothing in `spec.targets` references them, so
  `nebula-mesh.json` must be present. Resource- and data-source-level reads
  _are_ pruned, which is why `cluster/k8s/flux-system/gotk-components.yaml`
  (`filesha256` in `null_resource.flux_bootstrap`'s triggers, 380 KB) and
  `talos-cloud-controller-manager/helmrelease.yaml` (`data.helm_template`) stay
  out. If a future change makes either reachable, the plan will name the file
  and it needs its own `!/…` line.
- **gitignore cannot re-include a path under an excluded parent.** `/*`
  followed by `!/cluster/terraform` alone silently yields nothing; each parent
  needs its own `!/cluster` + `/cluster/*` pair.

## Exercised, still not green

Two failures so far, both loud and harmless — `planOnly` means nothing can be
applied whatever happens:

1. `terraform path not found` — the shared source, fixed by `infra-drift-source`.
2. `Invalid function argument` on `nebula.tf:17` — the root-level
   `nebula-mesh.json` missing under the first (`sparseCheckout`) form of that
   source, fixed by the `ignore` rules above.

Confirmed working along the way: `tofu init` reaches the registry and installs
`ovh/ovh`, which no other `Terraform` CR in this cluster uses. What the plan
itself reports is still unobserved.
