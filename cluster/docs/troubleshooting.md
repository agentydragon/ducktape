# Cluster Troubleshooting

## Talos Node Issues

### talosctl upgrade: Hostname Loss

**Signal**: `talosctl upgrade` is followed by random node names or duplicate entries in
`kubectl get nodes`. The incident also exposed a destructive `tofu apply -target
-auto-approve` replacement risk. See the
<lessons_learned/2026_03_07_talosctl_upgrade_hostname_loss.md> for platform-specific
configuration and the full-plan warning.

### Stale podCIDR After Node Hostname Change

**Symptoms**: Pods can't reach ClusterIP services; Longhorn `ManagerPodDown`; cascade to
ESO secret sync. Pods have IPs outside node's `spec.podCIDR`.

**Fix**: Delete pods with old-CIDR IPs -- DaemonSets recreate with correct IPs.

```bash
kubectl get nodes -o custom-columns='NAME:.metadata.name,CIDR:.spec.podCIDR'
kubectl get pods -A --field-selector spec.nodeName=<node> \
  -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,IP:.status.podIP'
```

See <lessons_learned/2026_03_07_talosctl_upgrade_hostname_loss.md> (Root Cause 3).

### Zombie Kubelet (Containerd Crash Recovery)

**Signal**: Node is `Ready` but pods stay `Pending`; kubelet is `STATE: Failed` with
`cannot delete running task kubelet: failed precondition` after a containerd crash.
See <lessons_learned/2025_11_17_zombie_kubelet_dual_ip.md> for the dual-IP cause,
diagnosis, and reboot recovery.

### Worker Dual-IP Assignment (DHCP + Static IP)

**Signal**: A worker has two `eth0` addresses, repeated `node IP skipped` messages, or
kubelet instability. The same lesson covers the cause, the `dhcp: false` correction,
diagnosis, and recovery: <lessons_learned/2025_11_17_zombie_kubelet_dual_ip.md>.

### XFS Quotacheck Stuck After Unclean Shutdown (Boot Hangs)

**Signal**: Talos stays at `STAGE: Booting`, `READY: False`, with `XFS (sda5): Quotacheck
needed` and no completion. `qm stop` caused the incident; always use `qm shutdown` for a
Talos VM. The recovery wipes EPHEMERAL, so first verify healthy etcd peers can restore the
node's membership.

```bash
talosctl -n <vlan-ip> -e <vlan-ip> --talosconfig terraform/main/talosconfig.yml \
  reset --system-labels-to-wipe EPHEMERAL --reboot --graceful=false
# If GPT drop fails (device busy), hard-reset from Proxmox:
ssh root@atlas "qm reset <vmid>"
```

See <lessons_learned/2026_03_24_xfs_quotacheck_stuck_boot.md> for the incident evidence
and recovery outcome.

## Cilium Issues

### Gateway Controller Dies After Talos Machine Config Apply

**Symptoms**: New HTTPRoutes have empty `status`; existing routes keep working;
Cilium operator logs: `"failed to wait for secret caches to sync"`.

**Cause**: Gateway controller is one-shot; if kube-apiserver restarts during init,
it dies permanently for that pod's lifetime.

**Fix**:

```bash
kubectl rollout restart deployment/cilium-operator -n kube-system
```

`bootstrap.py` does this automatically after `tofu apply`. For manual applies, run
the fix above.

**Diagnosis**:

```bash
kubectl get httproute -A -o json | python3 -c "
import sys, json
for r in json.load(sys.stdin)['items']:
    if not r.get('status', {}).get('parents'):
        print(f'{r[\"metadata\"][\"namespace\"]}/{r[\"metadata\"][\"name\"]}: no status')
"
```

### MTU Case Sensitivity (Cross-Node Packet Loss)

**Signal**: Cross-node TCP failures, webhook timeouts, bootstrap stalls, or `ReasmFails`
in `/proc/net/snmp`. Cilium's Helm key is uppercase `MTU` and sets the underlay: the
current <network.md> model uses `MTU: 1420` for pod MTU 1370. Do not copy the incident
guide's historical `MTU: 1370` setting; see <network.md> for current values and safe
application, and <lessons_learned/2026_02_11_cilium_mtu_cross_node_packet_loss.md> for
the incident diagnostics.

### `Policy denied` / ClusterIP Timeout During Control-Plane Instability (usually transient)

**Signal**: A Flux controller times out reaching a ClusterIP while
`cilium-dbg monitor --type drop` reports `Policy denied`, often during apiserver/etcd
instability. Verify the source identity should be allowed (`cilium-dbg endpoint get <id>`,
`identity list`): a drop from an unrelated pod is correct enforcement. Check current
control-plane health (`kubectl logs ... | grep "etcdserver: request timed out"`); do not
assume the historical HDD issue is present. See the “Compounding Factor” in
<lessons_learned/2026_07_03_tofu_controller_runner_rpc_hang.md>.

## tofu-controller Issues

### TLS Secret Cache Desync (Startup GC Bug)

**Signal**: Runner pods are `CrashLoopBackOff` with `secrets "terraform-runner.tls-<id>" not found`,
while tofu-controller says `TLS already generated for`. The dated lesson owns the startup-
GC/cache cause and full restart recovery, including the suspend/restart/resume fallback:
<lessons_learned/2025_11_19_tofu_controller_tls_cache_desync.md>. This cache failure is
distinct from the runner-Init reconcile hang below.

### Reconcile Hang After a Node Reboot (Runner Died Mid-Init)

**Historical signal (tofu-controller v0.16.1–v0.16.4)**: After a worker reboot or
eviction, a `Terraform` CR remains `Ready=Unknown` / `Initializing`, logs stop after
`generated template`, and no `tf-runner` is `Running`. Check the deployed controller
version first; the dated lesson records affected versions, the later upstream fix, and
the recovery procedure: <lessons_learned/2026_07_03_tofu_controller_runner_rpc_hang.md>.

### PG Advisory Lock Survives Runner Node Reboot

**Signal**: After a runner-node reboot, tofu-controller cannot acquire a PostgreSQL
advisory lock. `tfstate.forceUnlock` cannot release a lock still owned by another session. Before
terminating a backend, prove it is an orphan on the **current CNPG primary**; see
<lessons_learned/2026_07_11_tofu_pg_orphaned_session_lock.md> for the state matrix, SQL,
and safe recovery.

The separate Kubernetes-backend stale-Lease failure mode is historical because all CRs
now use PG; see <lessons_learned/2026_03_18_tofu_controller_stale_state_locks.md>.

## Secrets & Auth Issues

### Resource ID Desync After Wiping a Backing Datastore

Generalization of the Authentik-DB-wipe failure mode (see
<lessons_learned/2026_02_18_authentik_tf_state_lifecycle_coupling.md>): any
tofu-controller `Terraform` CR that manages resources inside another stateful
system (Authentik DB, Forgejo DB, etc.) will go into
`Unable to read user/object … not found with id N` if that backing system is
wiped without also clearing the corresponding tofu state. The tfstate still
references the old numeric IDs.

Recent instances:

- `sso-providers` after Authentik DB wipes (the original instance).
- `forgejo-props` after the 2026-06-02 Forgejo recovery
  (<lessons_learned/2026_06_02_seaweedfs_volume_loss_ovh_rename.md>): the
  forgejo-db CNPG cluster was rebuilt, so `forgejo_user.props` lost its id=2.

Recovery (PG-backend CRs — current default):

1. `flux suspend kustomization -n flux-system <name>` on the affected CR's
   Kustomization (e.g. `forgejo-props`, `sso-providers`).
2. From a pod with PG access (`kubectl exec` into any tofu-state-db client,
   or `kubectl port-forward` to it), `DROP SCHEMA <cr_name> CASCADE;` on the
   `tofu-state` database. Each CR has its own schema named after the CR.
   This wipes the tfstate; the next reconcile re-creates resources from
   scratch.
3. Clean up any orphan objects in the backing system (e.g. delete the
   half-created Authentik provider, or any leftover Forgejo user/repo).
4. `flux resume kustomization -n flux-system <name>` and watch a fresh
   plan-and-apply.

The Kubernetes-backend secret deletion and Vault-based variants (retired 2026-04-19)
are historical; don't use those recovery steps. See
<lessons_learned/2026_02_18_authentik_tf_state_lifecycle_coupling.md> for the original
incident and historical procedure.

### SOPS Decryption Failure

**Signal**: Flux reports `sops decryption error`. See <secrets.md> § “SOPS Decryption
Failure in Flux” for verification and key-rotation recovery.

## PVC File Ownership After Restore

`kubectl cp` into a PVC creates files owned by root (uid 0). Most app containers
run as non-root (typically uid 1000). SQLite WAL mode needs write access to the DB
file _and_ the ability to create `-wal` and `-shm` siblings alongside it. If the app
container can't write to the restored files, it fails with `sqlite3.OperationalError:
attempt to write a readonly database` despite the healthz endpoint passing (it may
not touch the DB on startup).

**Fix**: After `kubectl cp`, run a root pod on the same PVC and:

```bash
chown <app-uid>:0 /data/*.db
chmod 644 /data/*.db
chmod 777 /data        # SQLite needs to create -wal/-shm files in the directory
```

**Prevention**: Use a restore pod with `runAsUser` matching the app's uid so files
are created with correct ownership from the start. If `kubectl cp` still creates as
root, pipe through `tar` inside the container with `su` or use an init container that
chowns before the app starts.

## Health Checks

### Nebula Mesh

```bash
talosctl -n <node-ip> logs ext-nebula          # Talos nodes
systemctl status nebula                         # NixOS workers
ping 10.42.0.15                                 # talos-kimsufi-cp-0 lighthouse
ping 10.42.0.13                                 # talos-kimsufi-worker-0 lighthouse
```

Port: UDP 4242. If down: check firewall, verify certs (`nebula-cert print -path /etc/nebula/host.crt`).

If rebooting both lighthouses leaves peers stuck (tunnels reported alive, no
re-handshake, decrypt failures), see
<troubleshooting/nebula_lighthouse_reboot_stale_tunnel.md>.

### DNS & cert-manager

```bash
# Check Route 53 records
dig allegedly.works A +short
dig api.allegedly.works A +short
dig allegedly.works NS

# Check the two reconcilers separately
kubectl -n ducktape-flux get kustomization external-dns external-dns-records
kubectl -n external-dns get dnsendpoint static-records
kubectl -n external-dns logs deployment/external-dns --since=15m
kubectl get workspace route53-domain-delegation  # domain registration/delegation only
```

**cert-manager DNS-01 failures**:

1. "propagation check failed: no such host" — DNS cache, wait for TTL expiry
2. "unable to assume role" / "AccessDenied" — check `aws-route53-credentials` secret in `cert-manager` namespace
3. Challenge TXT not created — check cert-manager logs, verify IAM permissions on Route 53 zone

**Force retry**: `kubectl delete challenge,order,certificaterequest -n <namespace> --all`

### Loki (Log Retrieval) — use this for logs of pods that no longer exist

**Reach for Loki first whenever the pod whose logs you want is gone.** `kubectl logs`
only works for live pods; the moment a pod is deleted, evicted, or its Job is reaped
(short `ttlSecondsAfterFinished`, `backoffLimit`/`activeDeadlineSeconds` failures,
rolled-out ReplicaSets, OOM-killed/CrashLoopBackOff pods that got replaced), its logs
are only in Loki. Alloy ships every pod's stdout/stderr to Loki, retained far longer
than the pods themselves — so for any postmortem of a failed Job, a crashed exporter, a
previous Deployment revision, etc., query Loki instead of giving up on "pod not found".

Loki runs in SimpleScalable mode — there is **no `deploy/loki`**. Query the read path
(`svc/loki-read:3100`) over the HTTP API. Port-forward + `curl` from your workstation:

```bash
kubectl -n loki port-forward svc/loki-read 3100:3100 &
END=$(date +%s); START=$((END-10800))   # last 3h
curl -sG http://localhost:3100/loki/api/v1/query_range \
  --data-urlencode 'query={namespace="agents-infra", pod=~"forgejo-token-rotation.+"}' \
  --data-urlencode "start=${START}000000000" --data-urlencode "end=${END}000000000" \
  --data-urlencode 'limit=300' --data-urlencode 'direction=forward'
```

Stream labels available: `namespace`, `pod`, `container`, `app`, `component`, `job`,
`node_name`, `filename`. List them with `/loki/api/v1/labels` and a label's values with
`/loki/api/v1/label/<name>/values`.

Gotchas:

- **Select pods by the `pod` label, not a line filter.** The pod name is a stream label,
  not part of the log text, so `{namespace="x"} |= "mypod"` returns nothing. Use
  `{namespace="x", pod=~"mypod.+"}`. `|=` / `|~` filter the message body only.
- **No logs in Loki ⇒ the container never started.** A pod that died in
  `ContainerCreating` (slow/failed image pull) or was killed before its entrypoint ran
  emits zero lines — an empty Loki result for it points at the scheduling/pull/mount
  phase, not the app. A Job whose `activeDeadlineSeconds` runs out during a cold image
  pull fails `DeadlineExceeded` with no streams at all.
- Timestamps in the API are **nanoseconds** (hence the `000000000` suffix).

Grafana also has Loki as a datasource for interactive log exploration (Explore view).

### Nix Cache

```bash
# Verify signing key
cd terraform/main && tofu output nix_signing_public_key && cd -

# In-cluster connectivity
kubectl run -it --rm debug --image=curlimages/curl:latest --restart=Never -- \
  curl http://harmonia.nix-cache.svc.cluster.local:5000/nix-cache-info
```

## A Failed Job Wedges Its Flux Kustomization

**Symptom.** A Kustomization sits unready indefinitely, and everything `dependsOn` it
stacks up behind `DependencyNotReady`:

```text
Ready=False (HealthCheckFailed): failed early due to stalled resources:
  [Job/<ns>/<name>-provisioner status: 'Failed']
```

Its `status.lastAttemptedRevision` moves with new commits while
`status.lastAppliedRevision` stays pinned to an old one — so a fix that lands in git
looks applied but never takes effect.

**Why it never self-heals.** Three things compound:

- `wait: true` makes Flux block on **every object it applies** (and ignore
  `healthChecks`), so the unified `plaid-mcp` Kustomization wedges on a Job as readily
  as on its CNPG `Cluster`.
- A Job's pod template is immutable, so re-applying an unchanged manifest is a no-op.
  Flux keeps applying; the Failed Job stays Failed.
- `kustomize.toolkit.fluxcd.io/force: enabled` recreates an object when apply hits an
  **immutable-field conflict** — i.e. when the manifest changed. It does nothing for a
  Job that failed for an environmental reason (network policy, dependency down) while
  its manifest stayed identical.

So a config fix in a _neighbouring_ file — the exact case in
[#3827](https://github.com/agentydragon/ducktape/pull/3827), where a
`CiliumNetworkPolicy` selector was blocking the provisioner's egress to Postgres — is
applied, correct, and completely inert.

**Recovery.** Delete the failed Job; Flux recreates it from git on the next reconcile:

```bash
kubectl -n <ns> delete job <name>
kubectl -n <flux-ns> annotate --overwrite kustomization <kustomization> \
  "reconcile.fluxcd.io/requestedAt=$(date -u +%FT%TZ)"
```

Nothing is lost — the Job already failed terminally and its definition lives in the repo.

**Prevention, and why it is not applied everywhere.** Idempotent repair/fetch
provisioners carry `ttlSecondsAfterFinished`, which deletes the finished Job so the next
reconcile recreates and re-runs it — recovery without a human. Change-driven scripts
(the `readonly-role` GRANT Jobs) deliberately do **not**: a TTL there would convert a
run-on-change script into a run-on-schedule one. Those stay on the manual recovery above.
Rationale lives next to the Jobs themselves — see
`HaMcpCredentialsProvisioner._add_job` in <../cdk8s/ha_mcp.py> and
`_readonly_provisioner` in <../cdk8s/study_casino/app.py>.

## Removing a CRD Operator (Uninstall Runbook)

Distilled from the Longhorn uninstall incident
(<lessons_learned/2026_05_13_longhorn_uninstall.md>). "Suspend the Flux resource

- delete some pods by hand" is **not**
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

5. **Helm uninstall hooks may stall on finalizers** whenever the controller is
   gone before its custom resources are — by hand, _or_ by pruning the operator
   and its CRs in the same GitOps commit, which is the easy way to hit this
   without ever running `kubectl delete` (#3607 stalled
   `kubectl delete ns openshell-system` exactly this way, on an
   `OpenShellProvider` whose `provider-cleanup` finalizer had no controller
   left). Identify and clear them:

   ```bash
   kubectl -n <ns> get <crd> -o jsonpath='{range .items[*]}{.metadata.name}: {.metadata.finalizers}{"\n"}{end}'
   kubectl -n <ns> patch <crd> <name> --type=merge -p '{"metadata":{"finalizers":[]}}'
   ```

   A stuck namespace names the culprit itself — check
   `kubectl get ns <ns> -o jsonpath='{.status.conditions}'` for
   `NamespaceContentRemaining` and `NamespaceFinalizersRemaining` rather than
   hunting. And clear the finalizer on the **resource**: patching the
   namespace's own `spec.finalizers` via the `/finalize` subresource is the
   widely-copied version of this fix and it orphans the namespace's contents in
   etcd.

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

A `kustomize build failed: ... no such file or directory` message there means a
resource in `cluster/k8s/flux/kustomization.yaml` or a generated Kustomization's
`spec.path` points at a path that doesn't exist in the tracked tree. Fix that path
(or recommit the missing directory), then all the queued-up downstream changes apply
at once.

The Bazel test `//cluster/validation:test_cluster_integration` catches dangling
resource references in `cluster/k8s/kustomization.yaml` (its orphaned-files and
dependency checks), preventing this class of silent wedge from reaching `devel`.

## Quick Reference

| Issue                                     | Fix                                                           |
| ----------------------------------------- | ------------------------------------------------------------- |
| Flux "no matches for kind"                | Restart kustomize-controller (usually auto-resolves)          |
| Node stuck NotReady "InvalidDiskCapacity" | Usually auto-resolves; restart VM if not                      |
| Node stuck Booting, quotacheck on sda5    | Wipe EPHEMERAL via `talosctl reset`, then `qm reset` from PVE |
