# Cluster Troubleshooting

## Talos Node Issues

### talosctl upgrade: Hostname Loss

**Symptoms**: After `talosctl upgrade`, nodes get random hostnames; duplicate entries in
`kubectl get nodes`.

**Fix**: Set `machine.network.hostname` explicitly in Terraform config patches.

See <lessons_learned/2026_03_07_talosctl_upgrade_hostname_loss.md>.

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

**Symptoms**: Node Ready but pods stuck Pending; kubelet `STATE: Failed`;
"cannot delete running task kubelet: failed precondition".

**Cause**: Containerd crash leaves orphaned kubelet process. Root cause was dual-IP
assignment on workers (fixed in commit 2bf6ae9 -- `dhcp: false` in worker machine config).

**Fix**: Reboot the node (`talosctl -n <node-ip> reboot`).

See <lessons_learned/2025_11_17_zombie_kubelet_dual_ip.md>.

### Worker Dual-IP Assignment (DHCP + Static IP)

**Symptoms**: Two IPs on eth0, "node IP skipped" in dmesg, kubelet instability.

**Cause**: Workers missing explicit `dhcp: false` -- DHCP assigns second IP.
**Fixed** in commit 2bf6ae9.

**Diagnosis**: `talosctl -n <ip> get addresses | grep "eth0.*10\."`

See <lessons_learned/2025_11_17_zombie_kubelet_dual_ip.md>.

### XFS Quotacheck Stuck After Unclean Shutdown (Boot Hangs)

**Symptoms**: Node stuck at `STAGE: Booting`, `READY: False`. All K8s components `n/a`.
`ext-nebula` and `ext-iscsid` waiting for `cri`. Nebula IP unreachable, VLAN IP reachable.
dmesg: `XFS (sda5): Quotacheck needed: Please wait.` with no completion.

**Cause**: Unclean shutdown (e.g., `qm stop` instead of `qm shutdown`) triggers XFS
quotacheck on EPHEMERAL. On large filesystems with many inodes (container images), the
quotacheck can hang indefinitely, blocking the boot sequencer at phase 5/9.

**Fix**: Wipe EPHEMERAL and hard-reset the VM. etcd replicates from healthy peers.

```bash
talosctl -n <vlan-ip> -e <vlan-ip> --talosconfig terraform/main/talosconfig.yml \
  reset --system-labels-to-wipe EPHEMERAL --reboot --graceful=false
# If GPT drop fails (device busy), hard-reset from Proxmox:
ssh root@atlas "qm reset <vmid>"
```

**Prevention**: Never use `qm stop` for Talos VMs — always `qm shutdown` (ACPI).

See <lessons_learned/2026_03_24_xfs_quotacheck_stuck_boot.md>.

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

**Symptoms**: 10-30% TCP failures between OVH and Proxmox; webhook timeouts; bootstrap
stalls; `ReasmFails` in `/proc/net/snmp`.

**Cause**: Cilium's Helm chart uses uppercase `MTU`, not `mtu`. Lowercase is silently
ignored, leaving pod MTU at 1500 and causing cross-node fragmentation/loss over the
VXLAN-in-Nebula stack.

**Fix**: Set `MTU: 1420` (uppercase) in `cilium-values.yaml`. Note `MTU` is the
**underlay** value, not the pod MTU — see <network.md> for the full
model (and the gotcha that `MTU: 1370` wrongly yields a 1320 pod MTU) and the live
apply procedure.

**Diagnosis**:

```bash
kubectl get configmap cilium-config -n kube-system -o yaml | grep -i mtu
kubectl exec -n kube-system ds/cilium -- ip link show cilium_vxlan
helm get values cilium -n kube-system
```

See <lessons_learned/2026_02_11_cilium_mtu_cross_node_packet_loss.md>.

### `Policy denied` / ClusterIP Timeout During Control-Plane Instability (usually transient)

**Symptoms**: A Flux controller (or similar) times out reaching a ClusterIP / pod
(`dial ... i/o timeout`, e.g. tofu-controller → source-controller `ArtifactFailed`);
`cilium-dbg monitor --type drop` shows `Policy denied` (`bpf_lxc.c` ingress). Often
coincident with etcd/apiserver `context deadline exceeded` and controllers losing leader
election.

**Do not immediately chase the datapath.** Two traps:

- A `Policy denied` drop from a pod that has **no business** reaching the target (e.g.
  `litellm` → `source-controller:9090`) is **correct enforcement**, not a fault — easy to
  misread as a blackhole. Verify the source's Cilium identity is one the target's ingress
  policy _should_ allow before suspecting Cilium (`cilium-dbg endpoint get <id>`,
  `identity list`).
- When the apiserver/etcd is unstable (the recurring etcd-on-HDD contention), Cilium
  identity/policy **realization lags**, transiently dropping traffic that is normally
  allowed. This self-resolves once etcd settles — check `kubectl logs ... | grep
"etcdserver: request timed out"` first.

**Fix**: Address the control-plane instability (etcd contention); the drops clear on their
own. See the "Compounding Factor" section of
<lessons_learned/2026_07_03_tofu_controller_runner_rpc_hang.md>.

## tofu-controller Issues

### TLS Secret Cache Desync (Startup GC Bug)

**Symptoms**: Runner pods CrashLoopBackOff with `secrets "terraform-runner.tls-XXX" not found`;
controller logs `"TLS already generated for"` but secrets don't exist.

**Cause**: Upstream bug -- startup GC deletes all TLS secrets (created before `time.Now()`),
but in-memory cache isn't cleared. See `mtls/rotator.go` in tf-controller source.

**Fix**:

```bash
# Restart controller to clear cache + delete stuck runners
kubectl rollout restart deployment/tofu-controller-tf-controller -n flux-system
kubectl wait --for=condition=available --timeout=60s deployment/tofu-controller-tf-controller -n flux-system
kubectl delete pods -n flux-system -l app.kubernetes.io/name=tf-runner
```

If that fails, suspend all Terraform resources first, restart, then resume.

See <lessons_learned/2025_11_19_tofu_controller_tls_cache_desync.md>.

### Reconcile Hang After a Node Reboot (Runner Died Mid-Init)

**Symptoms**: One or more `Terraform` CRs stuck `Ready=Unknown` / `Initializing`, not
advancing; controller logs go **silent** for those objects (their last line is
`"generated template"`); **no `tf-runner` pods `Running`** anywhere; stale `*-tf-runner`
pods left in `Error` (`phase=Failed`). Correlates with a worker-node reboot/eviction
(especially wyrm2 — all runners schedule there, so one reboot wedges the whole TF fleet).

**Cause**: Upstream bug — the reconcile goroutine parks **forever** in the runner `Init`
gRPC (`waitForReady:true` on a deadline-less context) when the runner pod dies
mid-reconcile; controller-runtime never re-runs that key, so it never self-heals and a
worker slot is leaked. Deleting the dead runner pods alone does **not** unwedge it.

**Fix** (restart the controller to clear the parked goroutines):

```bash
kubectl -n flux-system rollout restart deploy/tofu-controller
kubectl -n flux-system rollout status  deploy/tofu-controller --timeout=90s
kubectl -n flux-system delete pod -l app.kubernetes.io/name=tf-runner --field-selector status.phase=Failed
```

See <lessons_learned/2026_07_03_tofu_controller_runner_rpc_hang.md> (upstream fix:
`flux-iac/tofu-controller#1838`).

### PG Advisory Lock Survives Runner Node Reboot

PG advisory locks release when the owning **database session ends**, not merely when
Kubernetes reports the runner pod gone. A node reboot can drop the runner without sending
FIN/RST; PostgreSQL then retains the idle session and its advisory lock until TCP
keepalives detect the dead peer. The observed defaults waited two hours before the first
probe. Repeated `tfstate.forceUnlock` attempts do not evict a lock still owned by another
PostgreSQL session.

Query `pg_stat_activity` plus `pg_locks` on the **current CNPG primary**, prove the client
IP belongs to no live runner, then terminate only that orphaned backend with
`pg_terminate_backend(pid)`. See
<lessons_learned/2026_07_11_tofu_pg_orphaned_session_lock.md> for the state matrix and
exact diagnosis.

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

For the long-decommissioned `kubernetes` backend, the equivalent step was
`kubectl delete secret tfstate-default-<name> -n flux-system`. We don't run
that backend anymore — see <lessons_learned/2026_02_18_authentik_tf_state_lifecycle_coupling.md>
for the original write-up.

The pre-2026-04-19 Vault-based variants of this failure (ESO password-generator desync,
Vault version overwrites) are documented in <lessons_learned/> but no longer reachable —
Vault is decommissioned. Kept for context only.

### SOPS Decryption Failure

`sops decryption error` on a Kustomization means the cluster age key in
`flux-system/sops-age-cluster-secrets` doesn't match the key that encrypted the
`*.sops.yaml` files. See <secrets.md> § "SOPS Decryption Failure in Flux" for the
verify / `sops updatekeys` / redeploy fix (validated by `pre-commit run --all-files`).

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
  --data-urlencode 'query={namespace="augur", pod=~"budget-exporter.+"}' \
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
  phase, not the app. (This is exactly how the `budget-exporter` `DeadlineExceeded`
  failures presented: no streams, because the cold ~334 MB image pull dominated.)
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

A `kustomize build failed: ... no such file or directory` message there means
some `cluster/k8s/.../flux-kustomization.yaml` reference in
`cluster/k8s/kustomization.yaml` points at a path that doesn't exist in the
tracked tree. Fix the reference (or recommit the missing directory), then
all the queued-up downstream changes apply at once.

The Bazel test `//cluster/validation:test_cluster_integration` catches dangling
resource references in `cluster/k8s/kustomization.yaml` (its orphaned-files and
dependency checks), preventing this class of silent wedge from reaching `devel`.

## Quick Reference

| Issue                                     | Fix                                                           |
| ----------------------------------------- | ------------------------------------------------------------- |
| Flux "no matches for kind"                | Restart kustomize-controller (usually auto-resolves)          |
| Node stuck NotReady "InvalidDiskCapacity" | Usually auto-resolves; restart VM if not                      |
| Node stuck Booting, quotacheck on sda5    | Wipe EPHEMERAL via `talosctl reset`, then `qm reset` from PVE |
