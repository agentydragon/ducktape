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

**Split Blueprint Pattern**: Provider blueprint (`terraform/gitops/sso/{app}/`) creates OIDC
app in Authentik + stores credentials in Vault. Secret blueprint (`k8s/{app}/`) creates
ExternalSecret pulling from Vault into the app namespace.

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
