@README.md

# Agent Instructions

## ⚠️ CRITICAL: BOOTSTRAP TERMINOLOGY

"Bootstrap/tear down/recreate the cluster" means:

- **Default scope**: `tofu destroy` in `terraform/bootstrap/infrastructure/` → `bazel run //cluster:bootstrap`
- **Excluded by default**: `terraform/bootstrap/persistent-auth/` (keypairs, CSI tokens, signing keys)
- Only destroy persistent-auth when user explicitly says "including persistent auth" or "from scratch"

## ⚠️ CRITICAL: PERSISTENT AUTH PROTECTION

**NEVER destroy `bootstrap/persistent-auth` without explicit user authorization.**
Contains sealed secrets keypair and CSI tokens that survive VM teardown by design.

## ⚠️ CRITICAL: COMMIT BEFORE RECONCILE

**NEVER reconcile Flux resources until changes are committed AND pushed.** Flux reads from
the git remote, not your local filesystem.

## ⚠️ CRITICAL: AUTHENTIK TEARDOWN — REMAINING TF STATE

Most Authentik SSO config uses native blueprints (no TF state). Two Terraform modules
still target Authentik-adjacent systems:

- `tfstate-default-sso-secrets` — generates OAuth2 client secrets in Vault
- `tfstate-default-vault-oidc-auth` — configures Vault OIDC auth backend

After an Authentik DB wipe, these may need state cleanup:
`vault-oidc-auth` requires `vault auth disable oidc/` in Vault before re-apply.

See <docs/lessons_learned/2026-02-18-authentik-tf-state-lifecycle-coupling.md> for history.

## ⚠️ CRITICAL: VPS-ONLY RESILIENCE

**DNS and website MUST work/recover with VPS only (without Proxmox).** These services must
NOT depend on `proxmox-csi-retain` storage or Proxmox-pinned nodes. When adding or modifying
critical-path services (DNS, ingress, website, SSO), verify they use `hcloud-volumes` or
`local-path` storage and can schedule on VPS nodes. See <docs/plan.md> "VPS-Only Resilience
Invariants" for the full list and compliance tracking.

## PRIMARY DIRECTIVE: DECLARATIVE TURNKEY BOOTSTRAP

**Goal**: Committed repo state where `bazel run //cluster:bootstrap` → everything works.

1. **NO imperative patches** — all fixes must be committed configuration changes
2. **Development loop**: `tofu destroy` → `bazel run //cluster:bootstrap` → verify
3. **Debugging**: You CAN tinker with broken state to understand failures, but solutions MUST be declarative
4. **Done = destroy→bootstrap→verify passes** — working via manual patches is NOT done
5. **SSO required** for all in-scope applications (Authentik OIDC)

@docs/plan.md

### Debugging Broken Bootstrap

Investigate root cause (events, describe, flux kustomization status) and fix declarative config.
Common patterns: missing `dependsOn`, CRD not installed before instance, secret not deployed
before consumer.

## Bootstrap Script

**Only supported method**: `bazel run //cluster:bootstrap` — never run `tofu apply` directly.

Handles preflight validation (git clean, pre-commit, `tofu validate`), layered deployment
(Talos → Cilium → Flux), and sealed secrets across destroy/apply cycles.

**Sandbox**: Requires `dangerouslyDisableSandbox: true` and `timeout: 600000` (10 min).

**Timing**: ~15-20 min. Slowest: Proxmox disk import (7-9 min), K8s API wait (5-10 min).

## Testing

Always run the full cluster test suite after changes:

```bash
bazel test //cluster/...
```

This includes cluster validation scripts, Helm lint tests, and Terraform format/lint/validate
for all `tofu` modules under `cluster/`. When adding new Terraform modules, always create
`BUILD.bazel` targets for format, lint, and validate checks.

## Task Delegation

Delegate complex diagnostics, multi-step investigations, and independent workstreams to
subagents via the Task tool. Spawn agents in parallel when possible.

## SSO Integration

**Native Blueprint Pattern**: Authentik SSO providers, applications, outposts, and user config
are defined as native Authentik blueprints in `k8s/authentik/sso-blueprints.yaml` (ConfigMap
mounted into the worker). Blueprints re-apply every 60 min with `state: present` — idempotent,
no external state.

**Secret flow**: `terraform/gitops/sso-secrets/` generates all OAuth2 client secrets →
stores in Vault → ESO creates `authentik-sso-client-secrets` K8s Secret in authentik namespace
→ worker reads via `envFrom` → blueprints reference via `!Env` tags.

**App-side secrets**: Each app has an ESO in `k8s/authentik-blueprint/{app}-secret/` that
reads from the same Vault path, providing credentials to the application.

**Remaining Terraform**: `harbor-oidc-config/` (Harbor API), `vault-oidc-auth/` (Vault OIDC
auth backend) — these configure non-Authentik systems and still use TF state.

**Proxy-mode NetworkPolicy (required)**: When a service is behind the shared proxy outpost,
it trusts `X-authentik-username` / `X-authentik-groups` headers injected by the outpost. Any
pod that can reach the backend directly can forge those headers and impersonate any user. Add a
`networkpolicy.yaml` next to the service's kustomization, restricting ingress to the outpost pod:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: <service>-ingress
  namespace: <namespace>
spec:
  podSelector:
    matchLabels:
      <pod-label>: <value>
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: authentik
          podSelector:
            matchLabels:
              goauthentik.io/outpost-name: shared-proxy-outpost
      ports:
        - port: <backend-port>
          protocol: TCP
```

`namespaceSelector` + `podSelector` in the same `from` item are ANDed: only the outpost pod
in the `authentik` namespace passes, not the server/worker/db pods. Pod label verified against
the running cluster. Add `- networkpolicy.yaml` to the service's `kustomization.yaml`.

## Loki (Log Aggregation)

Loki collects logs from all pods via Promtail. Use it for postmortems when pod logs have
been lost (completed/deleted Jobs, crashed pods, evicted pods).

```bash
# Query logs by namespace and container (URL-encode the LogQL query)
START=$(date -d '1 hour ago' +%s)000000000
END=$(date +%s)000000000
kubectl exec -n loki loki-stack-0 -- wget -qO- \
  "http://localhost:3100/loki/api/v1/query_range?query=%7Bnamespace%3D%22NAMESPACE%22%2Ccontainer%3D%22CONTAINER%22%7D&limit=50&direction=backward&start=$START&end=$END"
```

Loki runs as `loki-stack-0` StatefulSet in the `loki` namespace. Promtail DaemonSet ships
logs from all nodes. Grafana has Loki as a datasource for interactive log exploration.

## Operational Context

- **SSH**: `root@atlas` (Proxmox host, key auth)
- **Talos CLI**: Run from cluster directory (direnv provides tools + config)
- **Proxmox API**: Only reachable from VLAN. Use `nodeSelector: topology.kubernetes.io/region: proxmox`.
- **Reference code**: `/code` using `domain.tld/org/repo` pattern

## Key Files

| File                       | Purpose                              |
| -------------------------- | ------------------------------------ |
| `hetzner-nodes.tf`         | VPS definitions                      |
| `proxmox-nodes.tf`         | Proxmox VM definitions               |
| `talos-machine-secrets.tf` | Machine secrets (ephemeral)          |
| `cilium.tf`                | CNI configuration                    |
| `main.tf`                  | Providers, firewall, Talos bootstrap |

## Secrets

@docs/secrets.md

## Troubleshooting

@docs/troubleshooting.md

@docs/lessons_learned/2025-11-28-eso-password-generator-desync.md

## Flux Kustomization Layering (CRD Dependencies)

**Never mix HelmReleases with CRD instances in the same Kustomization.** helm-controller
has a separate API cache that doesn't see CRDs from other controllers.

**Rule**: Layer 1 (CRD operators) → Layer 2 (`{app}-secrets/` with ESO resources) → Layer 3
(`{app}/` with HelmRelease). Each layer's `flux-kustomization.yaml` has `dependsOn` on the
previous. Violations detected by pre-commit (`validate_kustomizations.py`).

### When Adding New Applications

1. Create `{app}-secrets/` for ESO resources (`dependsOn: external-secrets-operator`)
2. Create `{app}/` for HelmRelease only (`dependsOn: {app}-secrets`)
3. Add cert-manager issuer toggle if app has TLS: `postBuild.substituteFrom` from
   `cert-manager-issuer-config` ConfigMap + `dependsOn: cert-manager-issuer-config`
