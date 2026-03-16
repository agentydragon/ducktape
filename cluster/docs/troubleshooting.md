# Cluster Troubleshooting Checklist

Quick diagnostic commands for common cluster issues.

## Known Issues

### talosctl upgrade: Hostname Loss

**Symptoms**:

- After `talosctl upgrade`, nodes register with random hostnames (e.g., `talos-6we-boc`)
- Old node entries remain as `NotReady,SchedulingDisabled`
- `kubectl get nodes` shows duplicate entries (old name + new random name)

**Root Cause**:

Talos derives hostnames from platform metadata (cloud-init, Hetzner metadata API). During
`talosctl upgrade`, the node kexecs into the new image but **does not re-read platform
metadata**. Without explicit `machine.network.hostname`, Talos generates a random hostname.

**Fix**: Always set `machine.network.hostname` explicitly in Terraform config patches:

```hcl
yamlencode({
  machine = {
    network = {
      hostname = each.value.name
    }
  }
})
```

**Lessons Learned**: See <lessons_learned/2026-03-07-talosctl-upgrade-hostname-loss.md>

### Hetzner VPS Accidental Replacement via tofu apply

**Symptoms**:

- `hcloud server list` shows different server IDs / IPs than expected
- Kubernetes API unreachable, etcd quorum lost
- `tofu plan` shows `must be replaced` due to `image` change

**Root Cause**:

Changing the Talos schematic rebuilds the Packer snapshot, changing the `image` ID on
`hcloud_server`. Without `image` in `lifecycle.ignore_changes`, Terraform plans a
destroy+recreate. Even targeted applies (`-target=...machine_configuration_apply...`)
resolve dependencies and pull in the server replacement.

**Fix**: `lifecycle { ignore_changes = [user_data, image] }` on `hcloud_server`.
Schematic changes are applied via `talosctl upgrade`, not server replacement.

**Prevention**: Never run `tofu apply -auto-approve` with `-target` flags without
reviewing the full plan first.

**Lessons Learned**: See <lessons_learned/2026-03-07-talosctl-upgrade-hostname-loss.md>

### Cilium Gateway Controller Dies After Talos Machine Config Apply

**Symptoms**:

- Newly created HTTPRoutes have empty `status` (no `Accepted`/`ResolvedRefs` conditions)
- Existing routes continue serving traffic normally
- New apps return `{"Message": "no app for hostname"}` or connection refused
- Cilium operator logs show: `"failed to wait for secret caches to sync: timed out waiting for cache to be synced for Kind *v1.Gateway"`

**Root Cause**:

The Cilium operator's gateway controller is a one-shot job. If it fails to sync the `*v1.Gateway`
cache during startup (e.g., kube-apiserver was restarting during `talos_machine_configuration_apply`),
it dies permanently for that pod's lifetime. Existing Envoy routes continue serving from last known
state, but new HTTPRoutes are never programmed.

This happens when `tofu apply` changes kube-apiserver or kubelet config (causing a rolling Talos
service restart), and the Cilium operator pod(s) happen to initialize during that window.

**Fix**:

```bash
kubectl rollout restart deployment/cilium-operator -n kube-system
kubectl rollout status deployment/cilium-operator -n kube-system --timeout=60s
```

**Prevention**: `bootstrap.py` now restarts the Cilium operator after `tofu apply` as part of
`deploy_infrastructure()`. For manual `tofu apply` runs that touch Talos machine config, run the
fix above.

**Diagnosis**:

```bash
# Check for HTTPRoutes with empty status (unaccepted by gateway controller)
kubectl get httproute -A -o json | python3 -c "
import sys, json
items = json.load(sys.stdin)['items']
for r in items:
    if not r.get('status', {}).get('parents'):
        print(f'{r[\"metadata\"][\"namespace\"]}/{r[\"metadata\"][\"name\"]}: no status (gateway controller dead?)')
"

# Check operator logs for the failure
kubectl logs -n kube-system -l name=cilium-operator --tail=50 | grep "gateway\|cache sync"
```

### Cilium MTU Case Sensitivity (Cross-Node Packet Loss)

**Symptoms**:

- 10-30% TCP connection failures between VPS and Proxmox nodes
- Webhook timeouts during bootstrap (Kyverno, ESO webhooks timing out with 5-10s deadlines)
- Bootstrap stalls at ~18/64 Ready kustomizations
- `kubectl exec` and API server calls intermittently fail with "context deadline exceeded"
- IP fragmentation statistics show hundreds of reassembly failures (`ReasmFails` in `/proc/net/snmp`)

**Root Cause**:

The Cilium Helm chart (v1.16.x) defines the MTU parameter as **uppercase** `MTU`, not lowercase `mtu`.
Helm values are case-sensitive — using `mtu: 1370` is silently ignored, leaving all pod interfaces
at the default MTU of 1500.

With VXLAN + KubeSpan (WireGuard), the maximum payload without fragmentation is:

- eth0 MTU (1500) - VXLAN overhead (50) - WireGuard overhead (80) = **1370 bytes**

When pod MTU is 1500 (wrong), VXLAN-encapsulated packets (up to 1550 bytes) exceed the kubespan
interface MTU (1420), forcing kernel fragmentation at the WireGuard interface. UDP fragments
traversing NAT/middleboxes between Hetzner VPS and home Proxmox are intermittently dropped.

**Diagnosis**:

```bash
# 1. Check if Cilium MTU was actually applied
kubectl get configmap cilium-config -n kube-system -o yaml | grep -i mtu
# Should show: mtu: "1370" — if missing, MTU was not applied

# 2. Check actual interface MTUs on a node
talosctl -n <node-ip> read /proc/sys/net/ipv4/ip_no_pmtu_disc
# Then check pod veth interfaces:
kubectl exec -n kube-system ds/cilium -- ip link show cilium_vxlan
# Should show: mtu 1370 — if 1500, MTU fix not applied

# 3. Check IP fragmentation counters (high numbers = problem)
talosctl -n <node-ip> read /proc/net/snmp | grep -E "^Ip:"
# Look for: FragOKs > 0, ReasmFails > 0 (fragmentation occurring)

# 4. Check Helm values actually deployed
helm get values cilium -n kube-system
# Look for: MTU: 1370 (uppercase) — not mtu: 1370 (lowercase)
```

**Resolution**:

In `terraform/bootstrap/infrastructure/cilium-values.yaml`, use **uppercase** `MTU`:

```yaml
# CORRECT (uppercase — matches Helm chart definition)
MTU: 1370

# WRONG (lowercase — silently ignored by Helm)
mtu: 1370
```

After fixing, destroy and re-bootstrap the cluster. Verify with `helm get values cilium -n kube-system`.

**Prevention**:

- Always check `helm show values cilium/cilium --version <version> | grep -i mtu` to verify
  the exact key name when upgrading Cilium versions
- The overhead calculation (VXLAN 50 + WireGuard 80 = 130, so 1500 - 130 = 1370) is documented
  in the values file comments

**Lessons Learned**: See <lessons_learned/2026-02-11-cilium-mtu-cross-node-packet-loss.md>

### Zombie Kubelet (Containerd Crash Recovery Failure)

**Symptoms**:

- Node shows Ready in `kubectl get nodes` but pods stuck in Pending
- `talosctl service kubelet status` shows: `STATE: Failed`, `HEALTH: Fail`, `service not running`
- Error: "cannot delete running task kubelet: failed precondition"
- Some pods (cilium, sealed-secrets) running but new pods cannot start
- CSI volume attachment succeeds but mount operations never happen

**Root Cause**:

- Containerd crashes (exit status 2) while kubelet is running
- Kubelet process and containerd-shim survive as orphaned processes
- Containerd restarts but loses tracking of the old kubelet container
- Talos service manager cannot delete the orphaned kubelet container to start new one

**Diagnosis**:

```bash
# Check service status
talosctl -n <node-ip> service kubelet status

# Look for zombie kubelet process
talosctl -n <node-ip> ps | grep kubelet

# Check events for failure pattern
talosctl -n <node-ip> events | grep kubelet

# Look for: "PREPARING: Creating service runner" → "FAILED: cannot delete running task"
```

**Resolution**:

1. **Reboot the affected node** (cleanest recovery):

   ```bash
   talosctl -n <node-ip> reboot
   ```

2. **Alternative** (riskier - may disrupt running pods):

   ```bash
   # Find zombie kubelet PID
   talosctl -n <node-ip> ps | grep kubelet

   # Force kill the process and shim
   talosctl -n <node-ip> kill <kubelet-pid>
   talosctl -n <node-ip> kill <shim-pid>
   ```

**Prevention**:

- **Fixed in commit 2bf6ae9**: Root cause was dual-IP assignment on workers (see below)
- Ensure workers have explicit `dhcp: false` in machine config
- Fix tf-runner crashloop to prevent container churn that triggers the issue

**Lessons Learned**: See <lessons_learned/2025-11-17-zombie-kubelet-dual-ip.md>

### Worker Dual-IP Assignment (DHCP + Static IP Conflict)

**Symptoms**:

- Worker nodes have two IPs on eth0 (check with `talosctl get addresses`)
- Constant "node IP skipped" messages in dmesg
- Kubelet restarts correlated with container creation/deletion
- Eventually leads to Zombie Kubelet state (see above)

**Diagnosis**:

```bash
# Check for dual IPs on workers
talosctl -n 10.2.2.1 get addresses | grep "eth0.*10\."

# Expected (good): single IP
# eth0/10.2.2.1/16

# Problem (bad): two IPs
# eth0/10.2.2.1/16
# eth0/10.0.98.85/16   ← DHCP-assigned, should not exist

# Check for NodeIPController confusion in dmesg
talosctl -n 10.2.2.1 dmesg | grep "node IP skipped"
```

**Root Cause**:

Workers were missing explicit network interface configuration:

- **Controllers**: Have `machine.network.interfaces` (for VIP) → DHCP implicitly disabled
- **Workers**: Had `network: {}` (empty) → DHCP enabled by default
- Network DHCP server assigns second IP to workers
- Talos NodeIPController sees both IPs, can't decide which is kubelet IP
- Every veth creation (container start) triggers re-evaluation
- Under sustained container churn, this destabilizes kubelet and crashes containerd

**Resolution**:

Fixed in OpenTofu by adding explicit interface config for workers:

```yaml
machine:
  network:
    interfaces:
      - interface: eth0
        dhcp: false
```

For existing clusters, either:

1. Re-run `tofu apply` (requires cluster recreate)
2. Manually patch via talosctl:

   ```bash
   talosctl -n <worker-ip> patch machineconfig --patch \
     '[{"op": "add", "path": "/machine/network/interfaces", "value": [{"interface": "eth0", "dhcp": false}]}]'
   ```

**Prevention**:

- Commit 2bf6ae9 adds `dhcp: false` to worker machine config
- New clusters created after this fix won't have the issue

### tofu-controller TLS Secret Cache Desync (Startup GC Bug)

**Symptoms**:

- Terraform runner pods in CrashLoopBackOff with `secrets "terraform-runner.tls-XXXXXXXX" not found`
- tofu-controller logs show: `"TLS already generated for"` but secrets don't exist
- `kubectl get secret -n flux-system -l app.kubernetes.io/name=tf-runner` returns no results
- Terraform resources stuck in "Reconciliation in progress" indefinitely
- Runner pod references specific TLS secret name in args but secret is missing

**Root Cause**:

**This is a bug in tf-controller's startup garbage collection logic.** The
`garbageCollectTLSCertsForcefully()` function uses `time.Now()` as the reference
point at controller startup, causing it to delete ALL pre-existing TLS secrets
(since they were created in the past). However, the in-memory cache
(`knownNamespaceTLSMap`) is not cleared, creating a desynchronization:

1. Controller starts, `referenceTime = time.Now()` (mtls/rotator.go:164)
2. Startup GC deletes all secrets where `CreationTimestamp.Before(referenceTime)` - which is ALL of them (line 325)
3. In-memory cache still has cached `TriggerResult` entries for each namespace
4. Existing runner pods still reference the now-deleted secret names
5. New reconciliation requests hit cache and return "TLS already generated" (line 264)
6. Runner pod starts, looks for TLS secret, crashes: "secrets not found"

**Code Location**: `github.com/weaveworks/tf-controller/mtls/rotator.go`

- Bug: Line 164 sets `referenceTime = time.Now()`
- Bug: Line 180-187 calls forceful GC with this reference time at startup
- Bug: Line 325 deletes secrets created before "now" (all existing secrets)
- Cache check: Line 255 returns cached result without verifying secret exists

**Diagnosis**:

```bash
# 1. Check if TLS secrets exist
kubectl get secret -n flux-system -l app.kubernetes.io/name=tf-runner
# Should be empty if bug hit

# 2. Check runner pod logs for specific error
kubectl logs -n flux-system <terraform-name>-tf-runner
# Look for: secrets "terraform-runner.tls-XXXXXXXX" not found

# 3. Check controller logs for cache hit
kubectl logs -n flux-system deployment/tofu-controller-tf-controller --tail=100 | grep "TLS already generated"
# Controller thinks TLS is generated but it's not

# 4. Check runner pod secret reference
kubectl get pod -n flux-system <terraform-name>-tf-runner -o yaml | grep tls-secret-name
# Shows which secret the runner is looking for

# 5. Verify the secret really doesn't exist
kubectl get secret -n flux-system <secret-name-from-above>
# Should return: Error from server (NotFound)
```

**Resolution**:

**Option 1: Restart tofu-controller** (forces cache rebuild):

```bash
kubectl rollout restart deployment/tofu-controller-tf-controller -n flux-system
# Wait for controller to restart and regenerate TLS secrets
sleep 30

# Force reconcile affected Terraform resources
kubectl annotate terraform <terraform-name> -n flux-system reconcile.fluxcd.io/requestedAt="$(date +%s)" --overwrite
```

**Option 2: Clear cache via controller restart and force regeneration**:

```bash
# 1. Restart controller to clear in-memory cache
kubectl rollout restart deployment/tofu-controller-tf-controller -n flux-system

# 2. Wait for controller to be ready
kubectl wait --for=condition=available --timeout=60s deployment/tofu-controller-tf-controller -n flux-system

# 3. Delete all stuck runner pods to trigger fresh reconciliation
kubectl delete pods -n flux-system -l app.kubernetes.io/name=tf-runner

# 4. Controller will regenerate TLS secrets on next reconciliation
```

**Option 3: Manual cache invalidation** (if Options 1-2 don't work):

```bash
# Suspend all Terraform resources to clear runner pods
kubectl get terraform -n flux-system -o name | xargs -I {} kubectl patch {} -p '{"spec":{"suspend":true}}' --type=merge

# Delete all runner pods
kubectl delete pods -n flux-system -l app.kubernetes.io/name=tf-runner

# Restart controller to clear cache
kubectl rollout restart deployment/tofu-controller-tf-controller -n flux-system
kubectl wait --for=condition=available --timeout=60s deployment/tofu-controller-tf-controller -n flux-system

# Resume Terraform resources
kubectl get terraform -n flux-system -o name | xargs -I {} kubectl patch {} -p '{"spec":{"suspend":false}}' --type=merge
```

**Prevention**:

- This is an upstream bug in tf-controller - no cluster-side prevention available
- Monitor runner pods for CrashLoopBackOff after controller restarts
- Consider filing issue upstream: <https://github.com/weaveworks/tf-controller/issues>

**Upstream Bug Report**: TODO - file issue with tf-controller project

**Proposed Fix**: Change `referenceTime` in rotator.go:164 to use controller start
time or `time.Now().Add(-cr.CAValidityDuration)` instead of `time.Now()`, so
startup GC only deletes genuinely expired secrets, not all existing ones.

**Lessons Learned**: See <lessons_learned/2025-11-19-tofu-controller-tls-cache-desync.md>

### ESO Password Generator Desynchronization (SSO Authentication Failures)

**Symptoms**:

- SSO/OIDC authentication fails with "invalid client credentials" or "unauthorized"
- Authentik terraform successfully creates OIDC provider with secret A
- Kubernetes secret contains different secret B (randomly generated)
- Application uses secret B, Authentik expects secret A
- ExternalSecret using ESO Password generator instead of Vault data source

**Root Cause**:

**ESO Password generators create passwords independently on each sync, not reading from a source of truth.**
When an application's SSO configuration uses two sources for the client secret:

1. Terraform blueprint generates `random_password.result` → stores in Vault at `kv/sso/{app}` → creates Authentik provider
2. ExternalSecret uses ESO Password generator → generates different password → puts in K8s secret
3. Authentik knows password A, application uses password B → authentication fails

**Diagnosis**:

```bash
# 1. Check if ExternalSecret uses Password generator (WRONG)
kubectl get externalsecret <app>-oidc-secret -n <namespace> -o yaml | grep -A5 "generatorRef"
# If you see "kind: Password" - this is the problem

# 2. Compare passwords in Vault vs K8s secret
# Get password from Vault
kubectl exec -n vault vault-0 -c vault -- \
  env VAULT_TOKEN=<token> vault kv get -field=client_secret kv/sso/<app>

# Get password from K8s secret
kubectl get secret <app>-oauth-client-secret -n <namespace> \
  -o jsonpath='{.data.client_secret}' | base64 -d

# 3. Check terraform blueprint generates password and stores in Vault
grep -A10 "random_password.*client_secret" terraform/gitops/sso/<app>/main.tf
grep -A10 "vault_kv_secret_v2.*oidc" terraform/gitops/sso/<app>/main.tf
```

**Resolution**:

Replace ESO Password generator with Vault data source. Example fix:

```yaml
# BEFORE (WRONG - generates independent password):
---
apiVersion: generators.external-secrets.io/v1alpha1
kind: Password
metadata:
  name: app-oauth-client-secret-generator
spec:
  length: 32
---
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: app-oauth-client-secret
spec:
  dataFrom:
    - sourceRef:
        generatorRef:
          kind: Password
          name: app-oauth-client-secret-generator


# AFTER (CORRECT - reads from Vault):
---
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: app-oidc-secret
  namespace: <app>
spec:
  secretStoreRef:
    name: vault-backend
    kind: ClusterSecretStore
  target:
    name: app-oauth-client-secret
  data:
    - secretKey: client_id
      remoteRef:
        key: sso/<app>
        property: client_id
    - secretKey: client_secret
      remoteRef:
        key: sso/<app>
        property: client_secret
```

**Prevention**:

- ALWAYS use Vault as single source of truth for SSO credentials
- NEVER use ESO Password generator for credentials managed by terraform
- Pattern: Terraform generates → stores in Vault → ESO reads from Vault
- Review: `k8s/authentik-blueprint/*/client-secret-eso.yaml` should NOT have Password generators

**Reference Implementation**:

- Correct pattern: `k8s/applications/gitea/secrets.yaml` (lines 38-60)
- Terraform blueprint: `terraform/gitops/sso/gitea/main.tf`

**Lessons Learned**: See <lessons_learned/2025-11-28-eso-password-generator-desync.md>

### Authentik API Token 403 (Vault Version Desync)

**Symptoms**:

- All tofu-controller Terraform resources using Authentik API return 403
- `authentik-blueprint-users`, `authentik-blueprint-gitea`, etc. stuck reconciling
- Cascading failures: SSO provider blueprints can't create/update OIDC applications

**Root Cause**:

Terraform runner crash during `authentik-token` module → state lost → tofu-controller
retries with fresh state → `random_password` generates new token → overwrites Vault.
Authentik DB has the original token (write-once via `state: created`), but Vault (and
thus K8s secrets via ESO) has the new one.

**Diagnosis**:

```bash
# 1. Check Authentik API with current token
TOKEN=$(kubectl get secret authentik-api-token -n flux-system \
  -o jsonpath='{.data.authentik_token}' | base64 -d)
kubectl exec -n authentik deployment/authentik-server -- \
  curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $TOKEN" http://localhost:9000/api/v3/core/groups/
# 403 = token mismatch

# 2. Check Vault secret version history
ROOT_TOKEN=$(kubectl get secret -n vault instance-unseal-keys \
  -o jsonpath='{.data.vault-root}' | base64 -d)
kubectl exec -n vault instance-0 -c vault -- sh -c \
  "VAULT_ADDR=https://127.0.0.1:8200 VAULT_CACERT=/vault/tls/ca.crt \
   VAULT_TOKEN=$ROOT_TOKEN vault kv metadata get kv/sso/client-secrets"
# Multiple versions = overwrite occurred

# 3. Check Terraform resource status
kubectl get terraform -n flux-system | grep -v Applied
```

**Resolution**:

```bash
# Roll Vault back to version 1 (the token Authentik DB recognizes)
ROOT_TOKEN=$(kubectl get secret -n vault instance-unseal-keys \
  -o jsonpath='{.data.vault-root}' | base64 -d)
kubectl exec -n vault instance-0 -c vault -- sh -c \
  "VAULT_ADDR=https://127.0.0.1:8200 VAULT_CACERT=/vault/tls/ca.crt \
   VAULT_TOKEN=$ROOT_TOKEN vault kv rollback -version=1 kv/sso/client-secrets"

# Force ESO re-sync
kubectl annotate externalsecret authentik-api-token -n flux-system \
  force-sync=$(date +%s) --overwrite

# Force reconcile stuck Terraform resources
kubectl annotate terraform -n flux-system --all \
  reconcile.fluxcd.io/requestedAt="$(date +%s)" --overwrite
```

**Prevention**: `cas = 0` on all write-once `vault_kv_secret_v2` resources prevents
silent overwrites when TF state is lost.

**Lessons Learned**: See <lessons_learned/2026-02-13-authentik-token-vault-overwrite.md>

### Authentik Teardown: Terraform State Desync (Mostly Historical)

**Status**: Most SSO config migrated to native Authentik blueprints (no TF state). Only
`sso-secrets` (Vault secret generation) and `vault-oidc-auth` (Vault OIDC backend) remain
as Terraform. The cascading multi-module desync described below should no longer occur.

**Symptoms** (historical, pre-blueprint migration):

- Terraform resources stuck with `"already exists"` errors after Authentik DB wipe
- Providers assigned to wrong applications (cross-contamination between SSO modules)
- `authentik-blueprint-users` fails with `"slug already exists"` or `"username must be unique"`
- Cascading failures: all `authentik-blueprint-*` kustomizations stuck False

**Root Cause**:

tofu-controller stores Terraform state in K8s secrets (`tfstate-default-*`). These secrets
reference Authentik resource PKs/UUIDs. When Authentik's database is wiped (HelmRelease +
PVC deleted), the PKs become invalid but the state secrets persist. Multiple modules
applying simultaneously against the fresh DB causes partial applies (resources created but
state not saved), leading to irrecoverable `"already exists"` errors.

**This is an inherent limitation** — tofu-controller has no lifecycle coupling between
state secrets and the managed backend. See
<lessons_learned/2026-02-18-authentik-tf-state-lifecycle-coupling.md> for full analysis.

**Diagnosis**:

```bash
# Check for stale TF state secrets
kubectl get secrets -n flux-system | grep tfstate | grep authentik

# Check for cross-assigned providers in Authentik
TOKEN=$(kubectl get secret authentik-api-token -n flux-system \
  -o jsonpath='{.data.authentik_token}' | base64 -d)
kubectl exec -n authentik deployment/authentik-server -- \
  curl -s -H "Authorization: Bearer $TOKEN" \
  'http://localhost:9000/api/v3/providers/all/' | \
  python3 -c "import sys,json; [print(f'{p[\"name\"]:25s} -> {p[\"assigned_application_slug\"]}') for p in json.load(sys.stdin)['results']]"
# If provider names don't match their assigned application slugs → cross-contamination
```

**Resolution**:

```bash
# 1. Suspend all Authentik-targeting Terraform resources
for name in authentik-blueprint-users authentik-blueprint-gitea \
  authentik-blueprint-harbor authentik-blueprint-hubble authentik-blueprint-loki \
  authentik-blueprint-matrix authentik-blueprint-vault authentik-blueprint-openclaw \
  grafana-sso vault-oidc-auth; do
  kubectl patch terraform "$name" -n flux-system \
    -p '{"spec":{"suspend":true}}' --type=merge 2>/dev/null
done

# 2. Kill runner pods and delete stale state
kubectl delete pods -n flux-system -l app.kubernetes.io/name=tf-runner
kubectl delete secret -n flux-system \
  tfstate-default-authentik-blueprint-users \
  tfstate-default-authentik-blueprint-gitea \
  tfstate-default-authentik-blueprint-harbor \
  tfstate-default-authentik-blueprint-hubble \
  tfstate-default-authentik-blueprint-loki \
  tfstate-default-authentik-blueprint-matrix \
  tfstate-default-authentik-blueprint-vault \
  tfstate-default-authentik-blueprint-openclaw \
  tfstate-default-grafana-sso \
  tfstate-default-vault-oidc-auth \
  --ignore-not-found

# 3. Clean up ALL applications, providers, outposts from Authentik API
# (they were created by partial applies with wrong assignments)
TOKEN=$(kubectl get secret authentik-api-token -n flux-system \
  -o jsonpath='{.data.authentik_token}' | base64 -d)

# Delete applications by slug
for slug in gitea grafana harbor hubble loki matrix vault; do
  kubectl exec -n authentik deployment/authentik-server -- \
    curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
    "http://localhost:9000/api/v3/core/applications/$slug/" 2>/dev/null
done

# Delete non-embedded outposts
kubectl exec -n authentik deployment/authentik-server -- \
  curl -s -H "Authorization: Bearer $TOKEN" \
  'http://localhost:9000/api/v3/outposts/instances/' | \
  python3 -c "
import sys, json, subprocess
for o in json.load(sys.stdin)['results']:
    if 'Embedded' not in o['name']:
        print(f'Deleting outpost: {o[\"name\"]}')
" 2>/dev/null

# Delete user, flow, brand (from users blueprint partial apply)
# Flow: DELETE by slug, not UUID
kubectl exec -n authentik deployment/authentik-server -- \
  curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
  'http://localhost:9000/api/v3/flows/instances/custom-authentication-flow/'
# Brand: query then delete by brand_uuid
# User: query then delete by pk

# 4. If Vault was NOT wiped, also disable its stale OIDC auth backend
ROOT_TOKEN=$(kubectl get secret -n vault instance-unseal-keys \
  -o jsonpath='{.data.vault-root}' | base64 -d)
kubectl exec -n vault instance-0 -c vault -- sh -c \
  "VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN=$ROOT_TOKEN \
   vault auth disable oidc/" 2>/dev/null || true

# 5. Unsuspend Terraform resources
for name in authentik-blueprint-users authentik-blueprint-gitea \
  authentik-blueprint-harbor authentik-blueprint-hubble authentik-blueprint-loki \
  authentik-blueprint-matrix authentik-blueprint-vault authentik-blueprint-openclaw \
  grafana-sso vault-oidc-auth; do
  kubectl patch terraform "$name" -n flux-system \
    -p '{"spec":{"suspend":false}}' --type=merge 2>/dev/null
done
```

**Prevention**:

- **Always wipe TF state secrets when wiping Authentik DB** — run the suspend/delete/resume
  procedure above BEFORE Flux re-reconciles
- During full cluster rebuild (`tofu destroy` → bootstrap), all K8s secrets are destroyed
  with the cluster — no manual cleanup needed
- See <lessons_learned/2026-02-18-authentik-tf-state-lifecycle-coupling.md> for full details

## 🚨 Fast Path Health Checks

### KubeSpan (WireGuard Mesh) - VPS Hybrid Cluster

**Debug commands for KubeSpan mesh connectivity:**

```bash
# Primary debug - peer status (state should be "up")
talosctl -n <node-ip> get kubespanpeerstatuses -o yaml

# Peer specs (discovered endpoints)
talosctl -n <node-ip> get kubespanpeerspecs -o yaml

# Identity (WireGuard keys)
talosctl -n <node-ip> get kubespanidentities -o yaml

# Discovery members (both nodes should appear)
talosctl -n <node-ip> get members -o yaml
talosctl -n <node-ip> get affiliates -o yaml
```

**KubeSpan State Meanings:**

| State     | Meaning                                                    |
| --------- | ---------------------------------------------------------- |
| `unknown` | No endpoint set yet, or endpoint just changed (within 15s) |
| `up`      | WireGuard handshake within last ~275s                      |
| `down`    | No handshake for >275s                                     |

**Key Constants:**

- WireGuard port: UDP 51820
- PeerDownInterval: 275 seconds
- EndpointConnectionTimeout: 15 seconds

**If peers show `down`:** Check firewall allows UDP 51820, verify discovery service (`discovery.talos.dev:443`) reachable.

### Storage (Proxmox CSI) - Known Tricky Component

**Common Issues**: SealedSecret decryption failures, authentication errors with misleading messages.

CSI controller logs (`kubectl logs deployment/proxmox-csi-plugin-controller -n csi-proxmox`)
showing "401 Unauthorized" usually means the token is missing in Proxmox, not an auth config error.

```bash
# Check if secret has correct content
kubectl get secret proxmox-csi-plugin -n csi-proxmox -o jsonpath='{.data.config\.yaml}' | base64 -d

# If SealedSecret shows decryption error, regenerate with stable keypair:
cd terraform/bootstrap/persistent-auth
CSI_TOKEN_SECRET=$(tofu output -raw csi_token_secret)
cat > /tmp/csi-config.yaml << EOF
clusters:
- insecure: false
  region: "cluster"
  token: "kubernetes-csi@pve!csi=$CSI_TOKEN_SECRET"
  token_id: "kubernetes-csi@pve!csi"
  token_secret: "$CSI_TOKEN_SECRET"
  url: "https://atlas.agentydragon.com/api2/json"
EOF

kubectl create secret generic proxmox-csi-plugin \
  --namespace=csi-proxmox \
  --from-file=config.yaml=/tmp/csi-config.yaml \
  --dry-run=client -o yaml | \
kubeseal --cert <(tofu output -raw sealed_secrets_cert_pem) \
  --format=yaml | kubectl apply -f -

rm /tmp/csi-config.yaml
cd -

# 7. Check if CSI token exists in tofu state (managed by bpg/proxmox provider)
cd terraform/bootstrap/persistent-auth
tofu state show 'proxmox_virtual_environment_user_token.persistent["csi"]'
# Should show token details; if missing, run tofu apply in persistent-auth
cd -
```

## 🔧 Stable SealedSecret Keypair Issues

### Offline Validation (Pre-commit / Bootstrap)

**Validate all SealedSecrets offline before deployment:**

```bash
bazel run //cluster/validation:validate_sealed_secrets
```

This uses `kubeseal --recovery-unseal` to verify each SealedSecret in the repo can be decrypted
with the tofu keypair. No cluster access needed.

**When to run:**

- Automatically by pre-commit hook and bootstrap.py
- Manually after `tofu apply` in `bootstrap/persistent-auth`
- When debugging SealedSecret decryption failures

### Keypair Mismatch (Common Failure Mode)

**Symptoms:**

- Controller logs: `no key could decrypt secret`
- SealedSecrets status shows decryption error
- Pods pending due to missing secrets

**Cause:** SealedSecrets in git were sealed with a different keypair than what's currently
in tofu state (e.g., after tofu state was recreated).

**Quick Fix:**

```bash
cd terraform/bootstrap/persistent-auth && tofu apply
# This re-seals all SealedSecrets with current keypair
git add ../k8s/**/*sealed*.yaml && git commit -m "chore: re-seal secrets"
```

### Keypair Verification

```bash
# Check if stable keypair exists in tofu state
cd terraform/bootstrap/persistent-auth
tofu output sealed_secrets_cert_pem >/dev/null && echo "✅ Keypair exists in tofu state"

# Check if cluster is using stable keypair (serial numbers should match)
kubectl get secret sealed-secrets-key -n kube-system -o jsonpath='{.data.tls\.crt}' | \
  base64 -d | openssl x509 -text -noout | grep -A2 "Serial Number"
tofu output -raw sealed_secrets_cert_pem | openssl x509 -text -noout | grep -A2 "Serial Number"
cd -
```

### SealedSecret Decryption Test

```bash
# Test if a SealedSecret can be decrypted with stable keypair
cd terraform/bootstrap/persistent-auth
kubectl get sealedsecret <name> -n <namespace> -o yaml | \
kubeseal --recovery-unseal --recovery-private-key <(tofu output -raw sealed_secrets_private_key_pem)
# Should output the original secret YAML if working
cd -
```

### Creating New SealedSecrets

Always use the helper script to ensure correct keypair:

```bash
kubectl create secret generic my-secret --from-literal=key=value \
  --dry-run=client -o yaml | ./scripts/seal-secret.sh /dev/stdin k8s/path/my-sealed.yaml
git add k8s/path/my-sealed.yaml && git commit
```

## 🐛 Known Issues

### Proxmox CSI Storage

- **Issue**: SealedSecret decryption failures
- **Cause**: OpenTofu generating secrets with wrong keypair
- **Fix**: Always use stable keypair from tofu state (bootstrap/persistent-auth) when sealing

### Flux CRD Caching

- **Issue**: "no matches for kind" errors after CRD deployment
- **Cause**: Controller cache doesn't auto-refresh for new CRDs
- **Fix**: Restart kustomize-controller (usually resolves automatically)

### Headscale OIDC Split-Brain (Multi-Replica)

- **Issue**: OIDC authentication fails intermittently when running >1 replica
- **Cause**: OIDC state (nonces, PKCE verifiers) stored in per-process memory, not DB.
  Callback hitting a different replica than the initial redirect loses the state.
  Multi-replica also breaks node map updates, IP allocation, and route primary election.
- **Fix**: Run exactly 1 replica. This is a permanent architectural constraint, not a bug.
- **Lessons Learned**: <lessons_learned/2026-03-07-headscale-single-replica-only.md>

### Worker Node Kubelet Issues

- **Issue**: Node stuck NotReady with "InvalidDiskCapacity"
- **Cause**: Kubelet disk detection problems
- **Fix**: Usually resolves automatically, or restart VM

### DNS & Certificate Manager

#### PowerDNS

PowerDNS runs directly in the Kubernetes cluster on VPS nodes with `hostNetwork: true`,
binding to public IPs. There is no separate secondary DNS server or AXFR replication.

**Verify DNS**:

```bash
# Check zone contents
kubectl exec -n dns-system deployment/powerdns -- pdnsutil list-zone allegedly.works

# Verify NS records resolve externally
dig @ns1.allegedly.works allegedly.works NS
dig @ns2.allegedly.works allegedly.works NS
```

**Historical note**: The old architecture used AXFR replication from cluster PowerDNS to a
separate Docker-based PowerDNS on the legacy VPS at `agentydragon.com`. This was replaced
by the current architecture where VPS nodes are Kubernetes nodes running PowerDNS directly.
See <lessons_learned/2025-11-20-axfr-zone-transfer-failures.md> for historical context.

#### cert-manager DNS-01 Validation

**Common failures**:

1. **"propagation check failed: no such host"**
   - **Symptom**: cert-manager trying to resolve old NS record names
   - **Cause**: DNS cache still returning stale NS records
   - **Check**: `dig allegedly.works NS` (check TTL)
   - **Fix**: Wait for DNS cache expiry (typically 1 hour from NS change)

2. **"webhook call failed"**
   - **Check**: PowerDNS webhook pod running: `kubectl get pods -n cert-manager -l app.kubernetes.io/instance=pdns-webhook`
   - **Check**: PowerDNS API accessible:
     `kubectl exec -n cert-manager deployment/pdns-webhook -- wget -O-
http://powerdns-api.dns-system:8081/api/v1/servers`

3. **Challenge TXT record not created**
   - **Check**: PowerDNS logs: `kubectl logs -n dns-system deployment/powerdns`
   - **Check**: Webhook logs: `kubectl logs -n cert-manager -l app.kubernetes.io/instance=pdns-webhook`
   - **Verify**: API key secret exists: `kubectl get secret powerdns-api-key -n cert-manager`

**Force certificate retry**:

```bash
# Delete failed resources to trigger fresh attempt
kubectl delete challenge -n <namespace> --all
kubectl delete order -n <namespace> --all
kubectl delete certificaterequest -n <namespace> --all
# Certificate resource will recreate them automatically
```

### Nix Cache Issues

#### Signing Key Issues

```bash
# Verify key in tofu state
cd terraform/bootstrap/persistent-auth
tofu output nix_signing_public_key
# Output: cache.allegedly.works-1:BASE64KEY
cd -
```

**If signing key missing**: Re-run `tofu apply` in `terraform/bootstrap/persistent-auth`

#### In-Cluster Connectivity Test

```bash
kubectl run -it --rm debug --image=curlimages/curl:latest --restart=Never -- \
  curl http://harmonia.nix-cache.svc.cluster.local:5000/nix-cache-info
# Expected: StoreDir: /nix/store, WantMassQuery: 1, Priority: 30
```
