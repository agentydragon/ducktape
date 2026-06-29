# Vault Migration (completed 2026-04-19)

Historical record of the migration from Vault+ESO+TF secrets to SOPS or the
`sso-providers` TF pattern.

Vault was decommissioned. The KV store was emptied. The deployment was deleted.

## Secret Destinations

| Secret                                                                                      | New home                                          |
| ------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| SSO client secrets (Harbor, Matrix, Gitea, InvenTree, Grafana, Headlamp, OpenClaw, Airlock) | sso-providers TF -> k8s secret                    |
| Synapse signing key, macaroon, registration secret, redis pw                                | `matrix/secrets/*.sops.yaml`                      |
| Synapse admin credentials, openclaw-bot password                                            | `matrix/secrets/*.sops.yaml`                      |
| Harbor admin password                                                                       | `harbor/secrets/harbor-admin-initial.sops.yaml`   |
| Props evaluator password                                                                    | `props/secrets/evaluator-credentials.sops.yaml`   |
| Atuin credentials                                                                           | `user-agentydragon/atuin-user-password.sops.yaml` |
| Authentik user password                                                                     | `authentik/sso-secrets/user-password.sops.yaml`   |
| Authentik admin/secret-key/bootstrap token                                                  | `authentik/secrets/*.sops.yaml` (already was)     |

The remaining InvenTree unsuspension follow-up lives in <../k8s/TODO.md>.
