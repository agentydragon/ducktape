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
directories and this root reads a repo-root **file** (`nebula-mesh.json`).

### Which files the artifact must carry

**`-target` does not prune configuration evaluation.** It narrows which
resources the plan reports changes for; every expression in the root is still
evaluated, in `locals` and in resource arguments alike. So the rule is not
"what do the targets touch" but simply:

> every `file()`-family call in the root must resolve, however narrow
> `spec.targets` is.

All five call sites, and the file each needs:

| Call                                    | Where                    | Reads                                                         |
| --------------------------------------- | ------------------------ | ------------------------------------------------------------- |
| `nebula.tf:17` `jsondecode(file(…))`    | `locals`                 | `nebula-mesh.json` (repo root)                                |
| `talos-ccm.tf:14` `yamldecode(file(…))` | `locals`                 | `cluster/k8s/talos-cloud-controller-manager/helmrelease.yaml` |
| `flux.tf:23` `filesha256(…)`            | `null_resource` triggers | `cluster/k8s/flux-system/gotk-components.yaml`                |
| `flux.tf:24` `filesha256(…)`            | `null_resource` triggers | `cluster/k8s/flux-system/gotk-sync.yaml`                      |
| `cilium.tf:44` `filesha256(…)`          | `null_resource` triggers | `cilium-values.yaml`, inside the root module                  |

The `ignore` rules carry all of them. The rest of `cluster/k8s` (10 MB) is read
by nothing and stays out. `cluster/scripts/configure_game_mitigation.py` also
stays out: it is interpolated into a `local-exec` command string, not read by a
`file()` call, so nothing opens it at plan time.

`${path.module}/{kubeconfig,talosconfig.yml}` are Terraform-generated and never
in git. The `kubernetes` and `helm` providers point `config_path` at them and
tolerate their absence as long as no resource of theirs is planned — the same
property the first `-target` pass of `bazel run //cluster:bootstrap` relies on.

**gitignore cannot re-include a path under an excluded parent.** `/*` followed
by `!/cluster/terraform` alone silently yields nothing; each level needs its own
`!/cluster` + `/cluster/*` pair. Verify a change to these rules against `git` in
a scratch tree rather than reasoning about it.

## Exercised; four failures to get a plan

All missing paths, all loud and harmless — `planOnly` means nothing can be
applied whatever happens:

1. `terraform path not found` — the shared `flux-system` source omits
   `cluster/terraform/`; fixed by this dedicated source.
2. `nebula.tf:17` — `sparseCheckout` cannot express a repo-root file; fixed by
   switching to `ignore`.
3. `talos-ccm.tf:14` — second `file()` call, in a `locals` block.
4. `flux.tf:23-24` — two more, in resource `triggers`.

Rounds 3 and 4 were the same mistake twice: predicting that `-target` would
prune a call site instead of carrying every file the root reads. It does not
prune any of them. The table above is that enumeration, so the next `file()`
added to this root is a lookup rather than a fifth round.

Confirmed working along the way: `tofu init` reaches the registry and installs
`ovh/ovh`, which no other `Terraform` CR in this cluster uses.
