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

### Hetzner VPS Accidental Replacement via tofu apply

**Symptoms**: Different server IDs/IPs, API unreachable, etcd quorum lost.
`tofu plan` shows `must be replaced` due to `image` change.

**Fix**: `lifecycle { ignore_changes = [user_data, image] }` on `hcloud_server`.
Never `tofu apply -auto-approve` with `-target` without reviewing full plan.

See <lessons_learned/2026_03_07_talosctl_upgrade_hostname_loss.md>.

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

**Symptoms**: 10-30% TCP failures between VPS and Proxmox; webhook timeouts; bootstrap
stalls; `ReasmFails` in `/proc/net/snmp`.

**Cause**: Cilium Helm chart uses uppercase `MTU`, not `mtu`. Lowercase is silently
ignored, leaving pod MTU at 1500. VXLAN+Nebula needs 1412 (1500 - 50 - 38).

**Fix**: Use `MTU: 1412` (uppercase) in `cilium-values.yaml`, then destroy and re-bootstrap.

**Diagnosis**:

```bash
kubectl get configmap cilium-config -n kube-system -o yaml | grep -i mtu
kubectl exec -n kube-system ds/cilium -- ip link show cilium_vxlan
helm get values cilium -n kube-system
```

See <lessons_learned/2026_02_11_cilium_mtu_cross_node_packet_loss.md>.

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

### Stale State Locks (Runner Pod Death)

**Symptoms**: `error acquiring the state lock`; lock holder UUID matches the `holderIdentity`
of a `lock-tfstate-default-<name>` Lease in `flux-system`; lock-holding pod no longer exists;
runner pods churn every ~15s (ContainerCreating → Error → Terminating).

**Cause**: Anything that abruptly terminates a runner pod mid-plan leaves the K8s Lease's
`holderIdentity` set forever. The `kubernetes` terraform backend's locks have no TTL — the
controller never force-unlocks. Known triggers:

- `kubectl rollout restart` of the controller (combined with TLS cache desync — the new
  controller can't gRPC the orphaned runners to release locks)
- A node going `NotReady`/unreachable while runners were scheduled on it (e.g., wyrm2 outage
  on 2026-05-10 stranded ~14 locks; recovered 2026-05-14)
- Node drain / eviction / OOM-kill of a runner

**Diagnosis**:

```bash
# List Terraform CRs currently failing on a state lock
kubectl get terraform -n flux-system -o json | \
  jq -r '.items[] | select(.status.conditions[]? | .message | test("acquiring the state lock"; "i")) | .metadata.name'

# Confirm the Lease holder matches the lock UUID in the error and the pod is gone
kubectl get lease -n flux-system lock-tfstate-default-<name> -o yaml
kubectl get pod -n flux-system <name>-tf-runner  # should not exist or be a fresh Error pod
```

**Fix** — delete the stuck Lease(s) and trigger reconcile:

```bash
# Targeted (preferred — only the stuck ones)
stuck=$(kubectl get terraform -n flux-system -o json | \
  jq -r '.items[] | select(.status.conditions[]? | .message | test("acquiring the state lock"; "i")) | .metadata.name')
for name in $stuck; do
  kubectl delete lease -n flux-system "lock-tfstate-default-$name" --ignore-not-found
done
kubectl annotate terraform -n flux-system $stuck \
  reconcile.fluxcd.io/requestedAt="$(date +%s)" --overwrite

# Nuclear (clears every tfstate lock — all leases have label tfstate=true; safe because
# Leases are recreated on next acquisition and state is in tfstate-* secrets, not the Lease)
kubectl delete leases -n flux-system -l tfstate=true
kubectl annotate terraform -n flux-system --all \
  reconcile.fluxcd.io/requestedAt="$(date +%s)" --overwrite
```

After clearing, watch CRs flip from `False (acquiring the state lock)` to `True (Plan no
changes)`; a few may surface unrelated errors (`exit status 1`) that were masked by the
lock — diagnose those individually.

**Prevention**:

- Never `rollout restart` tofu-controller without first suspending all Terraform resources
  and deleting runner pods (see procedure in
  <lessons_learned/2026_03_18_tofu_controller_stale_state_locks.md>).
- Avoid scheduling tf-runners on flaky nodes. Runner pods inherit no nodeSelector by default
  — the controller schedules them wherever capacity exists, which can be a roaming/GPU
  worker. If a node frequently goes NotReady (e.g. wyrm2), expect periodic stranded locks
  until the upstream gains stale-lock detection.

See <lessons_learned/2026_03_18_tofu_controller_stale_state_locks.md>.

## Secrets & Auth Issues

### Authentik Teardown: TF State Desync

`tf/gitops/sso-providers/` owns Authentik OAuth2 providers. Its state secret
(`tfstate-default-sso-providers` in `flux-system`) references Authentik resource PKs.
Wiping Authentik's DB without also wiping that state secret causes the cascading desync
described in <lessons_learned/2026_02_18_authentik_tf_state_lifecycle_coupling.md>.

Recovery: suspend the `sso-providers` Terraform Kustomization, delete the stale
`tfstate-default-sso-providers` secret, clean up any half-created Authentik API objects,
unsuspend. See the lessons-learned doc for the full procedure.

The pre-2026-04-19 Vault-based variants of this failure (ESO password-generator desync,
Vault version overwrites) are documented in <lessons_learned/> but no longer reachable —
Vault is decommissioned. Kept for context only.

### SOPS Decryption Failure

**Symptoms**: Kustomization shows `sops decryption error`; secrets not created.

**Cause**: Cluster age key in `flux-system/sops-age-cluster-secrets` doesn't
match the key used to encrypt the `*.sops.yaml` files.

**Fix**:

```bash
# Verify the key exists
kubectl get secret sops-age-cluster-secrets -n flux-system

# Re-encrypt all cluster SOPS files with current keys
for f in $(find cluster/k8s -name '*.sops.yaml'); do sops updatekeys "$f"; done

# Redeploy the age key from tofu state
cd terraform/main && tofu apply -target=kubernetes_secret.sops_age_cluster_secrets
```

**Validation** (runs as part of unified pre-commit):

```bash
pre-commit run --all-files
```

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

On `hcloud-volumes`, `chown` may fail with "Operation not permitted" even from uid 0
(due to idmapped mounts). Workaround: `chmod 666` the DB files and `chmod 777` the
directory instead — the app gets write access via other/world bits.

**Prevention**: Use a restore pod with `runAsUser` matching the app's uid so files
are created with correct ownership from the start. If `kubectl cp` still creates as
root, pipe through `tar` inside the container with `su` or use an init container that
chowns before the app starts.

## Health Checks

### Nebula Mesh

```bash
talosctl -n <node-ip> logs ext-nebula          # Talos nodes
systemctl status nebula                         # NixOS workers
ping 10.42.0.1                                  # vps0 lighthouse
ping 10.42.0.2                                  # vps1 lighthouse
```

Port: UDP 4242. If down: check firewall, verify certs (`nebula-cert print -path /etc/nebula/host.crt`).

### Proxmox CSI

CSI "401 Unauthorized" usually means the token is missing in Proxmox, not an auth config error.

```bash
kubectl get secret proxmox-csi-plugin -n csi-proxmox -o jsonpath='{.data.config\.yaml}' | base64 -d
kubectl logs deployment/proxmox-csi-plugin-controller -n csi-proxmox
```

If the SOPS secret fails to decrypt, check the age key in `flux-system/sops-age-cluster-secrets`.
To verify the CSI token: `tofu state show 'proxmox_virtual_environment_user_token.persistent["csi"]'`.

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

### Loki (Log Retrieval)

Loki collects logs from all pods via Promtail. Useful for postmortems when pod logs have
been lost (completed Jobs, crashed/evicted pods).

```bash
START=$(date -d '1 hour ago' +%s)000000000
END=$(date +%s)000000000
kubectl exec -n loki deploy/loki -- wget -qO- \
  "http://localhost:3100/loki/api/v1/query_range?query=%7Bnamespace%3D%22NAMESPACE%22%2Ccontainer%3D%22CONTAINER%22%7D&limit=50&direction=backward&start=$START&end=$END"
```

Grafana also has Loki as a datasource for interactive log exploration.

### Nix Cache

```bash
# Verify signing key
cd terraform/main && tofu output nix_signing_public_key && cd -

# In-cluster connectivity
kubectl run -it --rm debug --image=curlimages/curl:latest --restart=Never -- \
  curl http://harmonia.nix-cache.svc.cluster.local:5000/nix-cache-info
```

## Quick Reference

| Issue                                     | Fix                                                           |
| ----------------------------------------- | ------------------------------------------------------------- |
| Flux "no matches for kind"                | Restart kustomize-controller (usually auto-resolves)          |
| Node stuck NotReady "InvalidDiskCapacity" | Usually auto-resolves; restart VM if not                      |
| Node stuck Booting, quotacheck on sda5    | Wipe EPHEMERAL via `talosctl reset`, then `qm reset` from PVE |
