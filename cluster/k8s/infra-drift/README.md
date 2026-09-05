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
| `data.sops_file.cluster_secrets_age`                                  | The cluster's master age key. It does not go into a runner pod.                                                                                                                                                                         |

Targeting is not just a scoping preference here: `cluster/k8s/TODO.md` records
that an untargeted `tofu plan` on this root still stalls during provider
refresh.

## Enabling

The Flux Kustomization ships `suspend: true` because the runner needs a
credential the cluster does not have, and minting it requires SOPS decryption.

1. **Narrow age key for the OVH secrets.** The `ovh` provider is configured
   from `secrets/ovh-credentials.sops.yaml`, and `ovh_dedicated_server.kimsufi`
   reads `secrets/ovh-rescue-ssh.sops.yaml`. Neither is encrypted to a key the
   cluster holds. Mint a single-purpose key rather than adding
   `&cluster-secrets` to them — same pattern as `&litellm-clients` in
   <../../../.sops.yaml>:

   ```bash
   age-keygen -o /tmp/infra-drift-age.key   # note the public half
   ```

   Add the public half to `.sops.yaml` as `&infra-drift`, list it in the
   `secrets/ovh-credentials\.sops\.yaml$` and
   `secrets/ovh-rescue-ssh\.sops\.yaml$` rules, then `sops updatekeys` both
   files. Store the private half as
   `cluster/k8s/infra-drift/sops-age-key.sops.yaml` (Secret
   `infra-drift-sops-age-key`, key `key`).

2. Add that filename to `kustomization.yaml`, drop `suspend: true` from
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
so the age key could not be minted and no runner pod has ever planned this
root. One thing to confirm on first unsuspend, which would show up as a failing
plan rather than as anything applied: `tofu init` in the runner pulls
`ovh/ovh`, which no existing `Terraform` CR in this cluster uses, so registry
reachability for that provider is unverified.
