# Vault Migration — Complete (2026-04-19)

All secrets migrated from Vault+ESO+TF to SOPS or sso-providers TF pattern.
Vault decommissioned. KV store emptied. Deployment deleted.

## What happened to each secret

| Secret                                                                                      | New home                                          |
| ------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| SSO client secrets (Harbor, Matrix, Gitea, InvenTree, Grafana, Headlamp, OpenClaw, Airlock) | sso-providers TF → k8s secret                     |
| Synapse signing key, macaroon, registration secret, redis pw                                | `matrix/secrets/*.sops.yaml`                      |
| Synapse admin credentials, openclaw-bot password                                            | `matrix/secrets/*.sops.yaml`                      |
| Harbor admin password                                                                       | `harbor/secrets/harbor-admin-initial.sops.yaml`   |
| Props evaluator password                                                                    | `props/secrets/evaluator-credentials.sops.yaml`   |
| Atuin credentials                                                                           | `user-agentydragon/atuin-user-password.sops.yaml` |
| Authentik user password                                                                     | `authentik/sso-secrets/user-password.sops.yaml`   |
| Authentik admin/secret-key/bootstrap token                                                  | `authentik/secrets/*.sops.yaml` (already was)     |

## Suspended services (migrate when unsuspending)

- **Gitea**: add SOPS secret for admin password (`kv/gitea/admin` value is gone — generate fresh)
- **InvenTree**: add SOPS secrets for admin + db passwords (same)
