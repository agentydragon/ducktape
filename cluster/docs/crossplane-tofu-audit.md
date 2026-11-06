# Crossplane and Kubernetes-secret OpenTofu audit

This records the September 2026 inventory used to decide whether Crossplane can
replace GitOps OpenTofu roots. It is not a migration instruction: retire a root
only after its replacement has reconciled and its consumers have passed their
functional checks.

## Crossplane coverage

The cluster does not install Crossplane. At the initial audit there were 22
`tf/gitops` roots. Three Kubernetes-secret roots have since been retired, so
there are now 19. Only two external APIs have a practical Crossplane path:

| Terraform resources                                                                                                                           | Roots                                                                                                                     | Assessment                                                                                                                                                                                             |
| --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `aws_route53_record`                                                                                                                          | `dns-records`                                                                                                             | A supported Crossplane Route53 `Record` resource exists. `aws_route53domains_registered_domain` has no equivalent in that provider's published resource list, so this root cannot move as a unit.      |
| `github_actions_secret`, `github_actions_variable`, `github_repository_environment`, `github_repository_ruleset`, `github_repository_webhook` | `github-secrets-sync`, `github-branch-protection`, `flux-webhook-token`                                                   | The community `provider-upjet-github` has equivalents, but its managed-resource APIs are `v1alpha1`. Treat the three roots as one optional migration after provider and credential-scoping evaluation. |
| `forgejo_*`                                                                                                                                   | `augur-evidence`, `budget-ledger`, `cpap-data`, `forgejo-agentydragon*`, `forgejo-claude`, `forgejo-images`, `haku-state` | No maintained Crossplane provider was identified.                                                                                                                                                      |
| `authentik_*`                                                                                                                                 | `agent-machine-access`, `alloy-otlp-bearer-token`, `gatus-sso`, `sso-providers`                                           | No maintained Crossplane provider was identified.                                                                                                                                                      |
| `litellm_*`, `claude-managed-agents_*`                                                                                                        | `litellm-keys`, `haku-cloud-agent`                                                                                        | No usable Crossplane provider was identified.                                                                                                                                                          |

Installing Crossplane's OpenTofu/Terraform provider would not address the
tofu-controller runner-RBAC problem. It would retain Terraform state and the
same provider credential model behind another controller.

## Completed Kubernetes-secret retirements

ESO is not an external-secret-manager replacement in this cluster. SOPS
remains the secret source of truth; ESO's Kubernetes provider copies or renders
in-cluster source Secrets. See `cluster/docs/decisions.md` under "Secrets: SOPS
SSOT".

### `ollama-bearer-token` — deployed in #6049

The Terraform `random_password` and Kubernetes Secret writer were replaced by
an ESO `Password` generator and `ExternalSecret` in `ollama`. The target keeps
the Reflector annotations that copy it to `claude-sandbox`; this deliberately
rotated the direct Ollama token.

The live check found the generator and ExternalSecret `Ready`, the reflected
Secret present, Ollama ready, and an authenticated request to the direct Ollama
endpoint returned HTTP 200. State cleanup is deliberately separate from this
source migration.

### `gaffer-private-ghcr-pull` — deployed in #6053

OpenTofu had only reshaped the SOPS-managed
`flux-system/github-pat-ghcr-read:token` into a Docker-config Secret. The
replacement renders `flux-system/gaffer-ghcr-pull` through ESO. A source
ServiceAccount can read only the SOPS PAT; a separate reader identity can read
only the rendered pull Secret. A `ClusterExternalSecret` distributes the pull
Secret to the consumer namespaces, replacing Reflector fanout.

The source ExternalSecret, SecretStores, and ClusterExternalSecret reconciled.
The source Docker-config Secret and the `thrive-scraper` copy had the expected
type. `augur` and `listing-monitor` did not exist during that check, so ESO will
create copies when those namespaces appear. State cleanup remains gated on the
final Flux health check.

### `litellm-api-key` — deployed in #6061

The master key and salt moved to SOPS-encrypted Secrets in
`cluster/k8s/litellm/secrets/`; the existing master key was preserved and the
salt was intentionally rotated. Gatus now references
`litellm-master-key:api-key` directly, eliminating the duplicate Gatus Secret.
The master-key Reflector fanout remains in place and can be evaluated for ESO
separately.

The salt rotation was authorized with the known database state: zero
credential rows, zero model rows, and nine LiteLLM virtual-key rows. The
remaining `litellm-keys` root still manages those external LiteLLM virtual keys
and teams, so it does not become a Kubernetes-secret retirement.

## Remaining sequence

1. Confirm the final Flux/workload health for #6053 and #6061, then remove the
   corresponding obsolete tofu-state database and credential resources.
2. Keep the remaining 19 roots on OpenTofu. The controller's runner service
   account still assumes broad cluster Secret access, so moving more roots
   there would not improve the security boundary.
3. If Crossplane is adopted, pilot only Route53 records after its provider and
   credential scope are reviewed. Evaluate the GitHub bundle independently;
   do not install Crossplane's OpenTofu provider as a workaround.
