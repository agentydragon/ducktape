# Terraform state databases

`tofu-state-db-ovh` hosts the shared `tfstate` database and a dedicated
`tfstate_ollama_bearer_token` database. CNPG owns the dedicated login through
`Cluster.spec.managed.roles` and the database through a `Database` resource.
The database is retained if its Kubernetes resource is deleted.

The Ollama login owns only its database and has no role memberships or administrative
attributes. Custom `pg_hba` rules require TLS for this login, reject its connections
to other databases, and reject other network logins into its database before CNPG's
default host rule. CNPG's fixed administrative and replication rules remain in place.
The shared `tfstate` login continues to serve the other Terraform roots.

The dedicated password is SOPS-managed in `db/ollama-bearer-token-credentials.sops.yaml`.
CNPG reconciles it into PostgreSQL; Reflector copies only this credential into
`ollama`. The shared database password is still reflected only into `flux-system`.
This isolates Ollama's database access; the existing cluster-wide permissions of
other Terraform runners are a separate follow-up under #5943.

Before switching a runner to the dedicated database, verify the Database's
`status.applied` and `status.observedGeneration`, the managed role status, and the
effective `pg_hba_file_rules` on the primary. Exercise these connections with the
actual credentials without printing them:

| Login                         | Database                                  | Expected |
| ----------------------------- | ----------------------------------------- | -------- |
| `tfstate_ollama_bearer_token` | `tfstate_ollama_bearer_token` over TLS    | Allowed  |
| `tfstate_ollama_bearer_token` | `tfstate_ollama_bearer_token` without TLS | Rejected |
| `tfstate_ollama_bearer_token` | `tfstate`, `postgres`                     | Rejected |
| `tfstate`                     | `tfstate_ollama_bearer_token`             | Rejected |
| `tfstate`                     | `tfstate`                                 | Allowed  |

Provisioning the database does not move state or rotate an application token.
