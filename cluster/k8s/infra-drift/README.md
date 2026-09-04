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

`spec.targets` scopes the plan to resources a runner pod can actually observe:
the OVH dedicated servers and their disk configuration, the two Proxmox VMs,
and the persistent Proxmox role/user/token.

Everything else in the root is excluded on purpose:

| Excluded                                                                     | Why                                                                                                                                                   |
| ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `local_file.{kubeconfig,talosconfig}`                                        | Written into the module directory on the operator's workstation. A fresh pod checkout does not have them, so every plan would report them as missing. |
| `kubernetes_*`, `helm_*`                                                     | Both providers are configured with `config_path = "${path.module}/kubeconfig"` — the file above.                                                      |
| `null_resource.*` bootstrap steps                                            | `local-exec` against `kubectl`, `helm` and `cilium` with that same kubeconfig.                                                                        |
| `talos_*`                                                                    | Machine config carries the Nebula node identities, whose private keys are per-host SOPS files.                                                        |
| `data.sops_file.cluster_secrets_age`                                         | The cluster's master age key. It does not go into a runner pod.                                                                                       |
| `ovh_dedicated_server_update.*_rescue`, `ovh_dedicated_server_reboot_task.*` | Provisioning one-shots, not steady state.                                                                                                             |

So this reports drift in the metal inventory, not in the bootstrap sequence.
Imperative `qm` edits on Atlas are the motivating case — `proxmox-vms.tf` says
in as many words that wyrm2's PCI passthrough is applied by hand and the file
"keeps TF in sync", which is exactly the divergence nothing currently watches.

To start narrower, drop the four `ovh_*` targets: the Proxmox slice needs no
SOPS key at all, only `infra-drift-proxmox-token`.

## Enabling

The Flux Kustomization ships `suspend: true` because the runner needs two
credentials the cluster does not have. Both steps need SOPS decryption, so they
are the operator's.

1. **Proxmox token.** `root@pam!tofu`, the same value `cluster/.envrc` reads
   from `secrets/shared/cluster-tokens.yaml`:

   ```bash
   cat > cluster/k8s/infra-drift/proxmox-token.sops.yaml <<EOF
   apiVersion: v1
   kind: Secret
   metadata:
     name: infra-drift-proxmox-token
     namespace: flux-system
   stringData:
     token: "root@pam!tofu=<uuid>"
   EOF
   sops -e -i cluster/k8s/infra-drift/proxmox-token.sops.yaml
   ```

2. **Narrow age key for the OVH secrets.** The `ovh` provider is configured
   from `secrets/ovh-credentials.sops.yaml`, and
   `ovh_dedicated_server.kimsufi` reads `secrets/ovh-rescue-ssh.sops.yaml`.
   Neither is encrypted to a key the cluster holds. Mint a single-purpose key
   rather than adding `&cluster-secrets` to them — same pattern as
   `&litellm-clients` in <../../../.sops.yaml>:

   ```bash
   age-keygen -o /tmp/infra-drift-age.key   # note the public half
   ```

   Add the public half to `.sops.yaml` as `&infra-drift`, list it in the
   `secrets/ovh-credentials\.sops\.yaml$` and `secrets/ovh-rescue-ssh\.sops\.yaml$`
   rules, then `sops updatekeys` both files. Store the private half as
   `cluster/k8s/infra-drift/sops-age-key.sops.yaml` (Secret
   `infra-drift-sops-age-key`, key `key`).

3. Add both filenames to `kustomization.yaml`, drop `suspend: true` from
   `flux-kustomization.yaml`, and commit.

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

## Not yet exercised

The manifests have not been run: this session could not decrypt any SOPS file,
so neither credential could be planted and no runner pod has ever planned this
root. Two things to confirm on first unsuspend, both of which would show up as
a failing plan rather than as anything applied:

- The `proxmox` provider declares `ssh { agent = true }`. Nothing in the target
  set needs SSH, but whether the provider tolerates a missing `SSH_AUTH_SOCK`
  at configure time is untested here.
- `tofu init` in the runner pulls `bpg/proxmox` and `ovh/ovh` from the
  registry; no existing CR in this cluster uses either.
