@README.md

## Talos Linux Documentation

Use `https://docs.siderolabs.com/llms.txt` as the entrypoint for Talos Linux
documentation. Fetch it with WebFetch to discover available doc pages.

# Agent Instructions

## CRITICAL: Bootstrap Terminology

"Bootstrap/tear down/recreate the cluster" means:

- **Default scope**: `bazel run //cluster:bootstrap` (single TF root at `terraform/main/`, uses targeted applies)
- **Persistent-auth resources** (keypairs, CSI tokens, signing keys) have `lifecycle { prevent_destroy = true }` in the merged root and are preserved across bootstrap cycles
- Only destroy persistent-auth resources when user explicitly says "including persistent auth" or "from scratch" (requires removing `prevent_destroy` lifecycle rules first)

## CRITICAL: Persistent Auth Protection

**NEVER remove `prevent_destroy` lifecycle rules on persistent-auth resources without explicit user authorization.**

## CRITICAL: Commit Before Reconcile

**NEVER reconcile Flux resources until changes are committed AND pushed.** Flux reads from
the git remote, not your local filesystem.

## CRITICAL: Authentik Teardown -- Remaining TF State

Most Authentik SSO uses native blueprints. Two TF modules still target Authentik-adjacent systems:
`tfstate-default-sso-secrets` (OAuth2 secrets in Vault) and `tfstate-default-vault-oidc-auth`
(Vault OIDC backend). After Authentik DB wipe, `vault-oidc-auth` requires
`vault auth disable oidc/` before re-apply.
See <docs/lessons_learned/2026-02-18-authentik-tf-state-lifecycle-coupling.md>.

## CRITICAL: VPS-Only Resilience

DNS and website MUST work with VPS only (without Proxmox). No `proxmox-csi-retain` storage
or Proxmox-pinned nodes. See <docs/plan.md> "VPS-Only Resilience Invariants".

## Primary Directive: Declarative Turnkey Bootstrap

**Goal**: `bazel run //cluster:bootstrap` from committed repo state produces a working cluster.

1. NO imperative patches -- all fixes must be committed configuration
2. Dev loop: `bazel run //cluster:bootstrap` -> verify (single TF root with targeted applies)
3. Debug freely, but solutions MUST be declarative
4. Done = bootstrap->verify passes
5. SSO required for all in-scope applications

@docs/plan.md

### Debugging Broken Bootstrap

Investigate root cause (events, describe, flux kustomization status) and fix declarative config.
Common patterns: missing `dependsOn`, CRD not installed before instance, secret not deployed
before consumer.

## Bootstrap Script

**Only supported method**: `bazel run //cluster:bootstrap`

Handles preflight validation, targeted applies against `terraform/main/` (persistent-auth ->
infrastructure -> full apply), sealed secrets. Requires `dangerouslyDisableSandbox: true`
and `timeout: 600000` (10 min). Takes ~15-20 min.

## Testing

Includes validation scripts, Helm lint, Terraform format/lint/validate. When adding new
Terraform modules, create BUILD.bazel targets for format, lint, and validate.

## Task Delegation

Delegate complex diagnostics and independent workstreams to subagents via the Task tool.

## SSO Integration

Native blueprints in `k8s/authentik/app/blueprints/` (ConfigMap, re-applied every 60 min).

**Secret flow**: `terraform/gitops/sso-secrets/` -> Vault -> ESO `authentik-sso-client-secrets`
in authentik namespace -> worker `envFrom` -> blueprint `!Env` tags.

**App-side secrets**: ESO in `k8s/authentik/blueprints/{app}-secret/` reads from same Vault path.

**Proxy-mode NetworkPolicy (required)**: When a service is behind the shared proxy outpost,
add a `networkpolicy.yaml` restricting ingress to the outpost pod. Without this, any pod can
forge `X-authentik-username` headers. Template:

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

`namespaceSelector` + `podSelector` in the same `from` item are ANDed.

**Deleting Authentik providers or applications**: Always add a `state: absent` tombstone
entry — never just remove the `state: present` block. The worker re-applies blueprints
every 60 min; the absent entry is what actually removes the stale resource. Follow the
`CLEANUP` tombstone convention from <../STYLE.md>. Place absent entries in the app's
existing blueprint, or in a dedicated cleanup blueprint (e.g.,
`k8s/authentik/blueprints/headscale-cleanup.yaml`) when the app itself is gone. Remove
the entries after a few reconcile cycles once confirmed clean.

## Operational Context

- **SSH**: `root@atlas` (Proxmox host, key auth)
- **Talos CLI**: Run from cluster directory (direnv provides tools + config)
- **Proxmox API**: Only reachable from VLAN. Use `nodeSelector: topology.kubernetes.io/region: proxmox`.

## Key Files

All in `terraform/main/`:

| File                       | Purpose                                        |
| -------------------------- | ---------------------------------------------- |
| `hetzner-nodes.tf`         | VPS definitions                                |
| `proxmox-nodes.tf`         | Proxmox VM definitions                         |
| `talos-machine-secrets.tf` | Machine secrets (ephemeral)                    |
| `cilium.tf`                | CNI configuration                              |
| `main.tf`                  | Providers, firewall, Talos bootstrap           |
| `persistent-auth.tf`       | Keypairs, tokens (`prevent_destroy` lifecycle) |

## Secrets

@docs/secrets.md

### Description Annotations

Add `metadata.annotations.description` to any resource where name + namespace doesn't
make the purpose obvious. Skip for obvious cases (sole deployment under a named
kustomization, SSO client secrets under `authentik/blueprints/`).

## CNPG (CloudNativePG)

@docs/cnpg-conventions.md

## Troubleshooting

@docs/troubleshooting.md

@docs/lessons_learned/2025-11-28-eso-password-generator-desync.md

## Harbor CI

Single `ducktape` project at `registry.allegedly.works/ducktape/<image>`.
Managed by `terraform/gitops/harbor-ci/main.tf`.

**Gotcha -- removing Harbor projects**: Use `removed` blocks with `lifecycle { destroy = false }`
to orphan from state (can't destroy projects with repositories without `force_destroy`).

**Gotcha -- Flux image automation race**: When renaming image paths, push at least one
image to the new path before updating `ImageRepository` resources. Otherwise Flux reverts
to the old path (old `ImageRepository` still finds tags).

## Flux Kustomization Layering

**Never mix HelmReleases with CRD instances in the same Kustomization.**

Layer 1 (CRD operators) -> Layer 2 (secrets with ESO) -> Layer 3 (app with
HelmRelease). Each layer's `flux-kustomization.yaml` has `dependsOn` on previous.
Violations detected by pre-commit (`validate_kustomizations.py`).

### k8s/ Directory Structure

**Flat** (single flux-kustomization): all manifests including `namespace.yaml`
live directly in the service directory. Example: `scanner/`, `gateway/`.

**Grouped** (multiple flux-kustomizations): subdirectories for each layer.
Example: `langfuse/{namespace,secrets,db,app}/`. A separate `namespace/`
subdir is needed only when multiple sibling kustomizations depend on the
namespace existing first.

**Agent infrastructure** lives under `agents/` (openclaw, airlock, claude-rbac,
tana-mcp, etc.).

### When Adding New Applications

**Simple service** (one flux-kustomization): create `k8s/{app}/` with all
manifests flat (namespace.yaml, deployment.yaml, service.yaml, etc.) and one
`flux-kustomization.yaml`.

**Service with secrets or database** (multiple flux-kustomizations):

1. Create `k8s/{app}/namespace/` with `namespace.yaml` + `flux-kustomization.yaml`
2. Create `k8s/{app}/secrets/` for ESO resources (`dependsOn: {app}-namespace, external-secrets-config`)
3. Create `k8s/{app}/app/` for workload (`dependsOn: {app}-namespace, {app}-secrets`)
4. Add cert-manager issuer toggle only if the app's own manifests reference
   `${LETSENCRYPT_ISSUER}` (not needed when TLS is handled by the gateway)
