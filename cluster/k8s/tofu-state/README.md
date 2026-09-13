# Terraform state databases

`tofu-state-db-ovh` hosts the shared `tfstate` database. CNPG owns the `tfstate`
login through `Cluster.spec.managed.roles`, and the shared password is
SOPS-managed in `db/credentials.sops.yaml`.

Terraform runner pods and `cluster/.envrc` read that Secret directly. The
database is shared by the remaining Terraform roots; application credentials
such as Ollama's direct bearer token are managed by their owning application
configuration.
