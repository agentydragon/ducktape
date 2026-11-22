# Terraform state databases

The `tofu-state-db` Flux Kustomization owns this Namespace and database together.
It depends on CNPG for admission; storage provisioning reconciles after admission.

`tofu-state-db-ovh` hosts the shared `tfstate` database. CNPG owns the `tfstate`
login through `Cluster.spec.managed.roles`, and the shared password is
SOPS-managed in `db/credentials.sops.yaml`.

Crossplane OpenTofu Workspaces and the remaining tofu-controller runners share
this database. Reflector copies the credential into `flux-system` for their
`PGPASSWORD` references. For local access, `cluster/.envrc` decrypts the source
file and sets `PG_CONN_STR`.
