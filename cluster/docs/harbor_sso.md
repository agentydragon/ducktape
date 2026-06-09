# Harbor SSO Configuration

## Approach Selection

| Option                        | Why not                                                       |
| ----------------------------- | ------------------------------------------------------------- |
| Harbor Helm chart values      | OIDC not configurable via `values.yaml`; post-deploy only     |
| Harbor CLI                    | Imperative, no drift detection                                |
| Harbor API (curl/Job)         | Imperative, requires custom wrapper, no drift detection       |
| Harbor Operator               | Does not exist (no official project)                          |
| Manual UI                     | Not declarative, violates turnkey bootstrap                   |
| **Harbor Terraform provider** | **Selected** — declarative, drift detection, existing pattern |

## Implementation

Uses the [goharbor/harbor Terraform provider](https://registry.terraform.io/providers/goharbor/harbor/latest/docs)
via tofu-controller.

**Location**: `tf/gitops/sso/harbor-config/`

The `harbor_config_auth` resource configures OIDC with Authentik (auto-onboard, group mapping).
Follows the same pattern as the other SSO providers in `tf/gitops/sso-providers/`.
