# Cluster Troubleshooting

## Talos Node Issues

### talosctl upgrade: Hostname Loss

**Symptoms**: After `talosctl upgrade`, nodes get random hostnames; duplicate entries in
`kubectl get nodes`.

**Fix**: Set `machine.network.hostname` explicitly in Terraform config patches.

See <lessons_learned/2026-03-07-talosctl-upgrade-hostname-loss.md>.

### Stale podCIDR After Node Hostname Change

**Symptoms**: Pods can't reach ClusterIP services; Longhorn `ManagerPodDown`; cascade to
Vault/ESO. Pods have IPs outside node's `spec.podCIDR`.

**Fix**: Delete pods with old-CIDR IPs -- DaemonSets recreate with correct IPs.

```bash
kubectl get nodes -o custom-columns='NAME:.metadata.name,CIDR:.spec.podCIDR'
kubectl get pods -A --field-selector spec.nodeName=<node> \
  -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,IP:.status.podIP'
```

See <lessons_learned/2026-03-07-talosctl-upgrade-hostname-loss.md> (Root Cause 3).

### Hetzner VPS Accidental Replacement via tofu apply

**Symptoms**: Different server IDs/IPs, API unreachable, etcd quorum lost.
`tofu plan` shows `must be replaced` due to `image` change.

**Fix**: `lifecycle { ignore_changes = [user_data, image] }` on `hcloud_server`.
Never `tofu apply -auto-approve` with `-target` without reviewing full plan.

See <lessons_learned/2026-03-07-talosctl-upgrade-hostname-loss.md>.

### Zombie Kubelet (Containerd Crash Recovery)

**Symptoms**: Node Ready but pods stuck Pending; kubelet `STATE: Failed`;
"cannot delete running task kubelet: failed precondition".

**Cause**: Containerd crash leaves orphaned kubelet process. Root cause was dual-IP
assignment on workers (fixed in commit 2bf6ae9 -- `dhcp: false` in worker machine config).

**Fix**: Reboot the node (`talosctl -n <node-ip> reboot`).

See <lessons_learned/2025-11-17-zombie-kubelet-dual-ip.md>.

### Worker Dual-IP Assignment (DHCP + Static IP)

**Symptoms**: Two IPs on eth0, "node IP skipped" in dmesg, kubelet instability.

**Cause**: Workers missing explicit `dhcp: false` -- DHCP assigns second IP.
**Fixed** in commit 2bf6ae9.

**Diagnosis**: `talosctl -n <ip> get addresses | grep "eth0.*10\."`

See <lessons_learned/2025-11-17-zombie-kubelet-dual-ip.md>.

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

See <lessons_learned/2026-02-11-cilium-mtu-cross-node-packet-loss.md>.

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

See <lessons_learned/2025-11-19-tofu-controller-tls-cache-desync.md>.

### Stale State Locks After Restart

**Symptoms**: `error acquiring the state lock`; lock holder references dead pod;
runner pods cycle every 15s.

**Cause**: `rollout restart` kills controller while runners hold locks. Orphaned runners
can't release locks due to TLS cache desync.

**Fix**:

```bash
kubectl delete leases -n flux-system -l tfstate=true
kubectl annotate terraform -n flux-system --all \
  reconcile.fluxcd.io/requestedAt="$(date +%s)" --overwrite
```

**Prevention**: Never `rollout restart` tofu-controller without first suspending all
Terraform resources and deleting runner pods.

See <lessons_learned/2026-03-18-tofu-controller-stale-state-locks.md>.

## Secrets & Auth Issues

### ESO Password Generator Desync (SSO Auth Failures)

**Symptoms**: SSO fails with "invalid client credentials". Vault has password A,
K8s secret has password B (independently generated).

**Cause**: ESO Password generators are stateless -- they generate fresh passwords on
each sync instead of reading from Vault. Must use Vault data sources instead.

**Fix**: Replace ESO Password generator with Vault data source in ExternalSecret.
Pattern: Terraform generates -> Vault -> ESO reads.

See <lessons_learned/2025-11-28-eso-password-generator-desync.md> for full analysis,
diagnosis commands, and correct pattern.

### Authentik API Token 403 (Vault Version Desync)

**Symptoms**: All Authentik-targeting Terraform resources return 403.

**Cause**: Runner crash -> state lost -> new `random_password` overwrites Vault.
Authentik DB has original token, Vault has new one.

**Fix**: Roll Vault back to version 1:

```bash
ROOT_TOKEN=$(kubectl get secret -n vault instance-unseal-keys \
  -o jsonpath='{.data.vault-root}' | base64 -d)
kubectl exec -n vault instance-0 -c vault -- sh -c \
  "VAULT_ADDR=https://127.0.0.1:8200 VAULT_CACERT=/vault/tls/ca.crt \
   VAULT_TOKEN=$ROOT_TOKEN vault kv rollback -version=1 kv/sso/client-secrets"
kubectl annotate externalsecret authentik-api-token -n flux-system \
  force-sync=$(date +%s) --overwrite
```

**Prevention**: `cas = 0` on write-once `vault_kv_secret_v2` resources.

See <lessons_learned/2026-02-13-authentik-token-vault-overwrite.md>.

### Authentik Teardown: TF State Desync (Mostly Historical)

Most SSO config migrated to native blueprints. Only `sso-secrets` and `vault-oidc-auth`
remain as Terraform. The cascading desync described in
<lessons_learned/2026-02-18-authentik-tf-state-lifecycle-coupling.md> should no longer occur.

If it does: suspend Authentik-targeting Terraform resources, delete stale `tfstate-default-*`
secrets, clean up Authentik API objects, unsuspend. See the lessons_learned doc for the
full procedure.

### SealedSecret Keypair Mismatch

**Symptoms**: `no key could decrypt secret`; SealedSecrets show decryption error.

**Cause**: SealedSecrets sealed with different keypair than current tofu state.

**Fix**:

```bash
cd terraform/bootstrap/persistent-auth && tofu apply
git add ../k8s/**/*sealed*.yaml && git commit -m "chore: re-seal secrets"
```

**Offline validation** (no cluster needed):

```bash
bazel run //cluster/scripts/validate_cluster:validate_sealed_secrets
```

**Keypair verification** (serial numbers should match):

```bash
# Committed cert
openssl x509 -noout -serial < k8s/sealed-secrets/sealed-secrets-cert.pem
# Cluster
kubectl get secret sealed-secrets-key -n kube-system -o jsonpath='{.data.tls\.crt}' | \
  base64 -d | openssl x509 -noout -serial
```

**Creating new SealedSecrets**: Always use the helper script:

```bash
kubectl create secret generic my-secret --from-literal=key=value \
  --dry-run=client -o yaml | ./scripts/seal-secret.sh /dev/stdin k8s/path/my-sealed.yaml
```

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

If SealedSecret decryption fails, regenerate with `tofu output` from `persistent-auth` and
re-seal. Check `tofu state show 'proxmox_virtual_environment_user_token.persistent["csi"]'`.

### DNS & cert-manager

```bash
kubectl exec -n dns-system deployment/powerdns -- pdnsutil list-zone allegedly.works
dig @ns1.allegedly.works allegedly.works NS
```

**cert-manager DNS-01 failures**:

1. "propagation check failed: no such host" -- DNS cache, wait for TTL expiry
2. "webhook call failed" -- check pdns-webhook pod and PowerDNS API reachability
3. Challenge TXT not created -- check PowerDNS + webhook logs, verify `powerdns-api-key` secret

**Force retry**: `kubectl delete challenge,order,certificaterequest -n <namespace> --all`

### Loki (Log Retrieval)

Loki collects logs from all pods via Promtail. Useful for postmortems when pod logs have
been lost (completed Jobs, crashed/evicted pods).

```bash
START=$(date -d '1 hour ago' +%s)000000000
END=$(date +%s)000000000
kubectl exec -n loki loki-stack-0 -- wget -qO- \
  "http://localhost:3100/loki/api/v1/query_range?query=%7Bnamespace%3D%22NAMESPACE%22%2Ccontainer%3D%22CONTAINER%22%7D&limit=50&direction=backward&start=$START&end=$END"
```

Grafana also has Loki as a datasource for interactive log exploration.

### Nix Cache

```bash
# Verify signing key
cd terraform/bootstrap/persistent-auth && tofu output nix_signing_public_key && cd -

# In-cluster connectivity
kubectl run -it --rm debug --image=curlimages/curl:latest --restart=Never -- \
  curl http://harmonia.nix-cache.svc.cluster.local:5000/nix-cache-info
```

## Quick Reference

| Issue                                     | Fix                                                  |
| ----------------------------------------- | ---------------------------------------------------- |
| Flux "no matches for kind"                | Restart kustomize-controller (usually auto-resolves) |
| Node stuck NotReady "InvalidDiskCapacity" | Usually auto-resolves; restart VM if not             |
