# Atuin Helm Chart

Helm packaging for the Atuin shell history sync server plus its PostgreSQL backing store.

## Features

- Configurable Atuin server deployment with configurable replicas, ingress, and optional NodePort service.
- Built-in PostgreSQL StatefulSet with PVC reuse to preserve existing data.
- Optional SealedSecret template for delivering the database credentials stored in git.
- Shared helpers from the `common-lib` chart for consistent labels and naming.

## Values Overview

| Key                  | Description                                      |
| -------------------- | ------------------------------------------------ |
| `image.*`            | Container image details for the Atuin server     |
| `service.port`       | Port the HTTP service exposes                    |
| `nodePortService.*`  | Controls creation of the NodePort service        |
| `ingress.*`          | Standard ingress configuration                   |
| `postgres.*`         | PostgreSQL image, storage, and resource settings |
| `secrets.postgres.*` | Secret naming and optional SealedSecret payload  |
| `config.serverToml`  | Override for the default `server.toml`           |

See `values.yaml` for full details.

## Usage

```bash
cd k8s/helm/atuin
helm dependency update
helm template atuin . --values values.yaml
helm upgrade --install atuin . --namespace default
```

To enable the SealedSecret, provide encrypted data in a values file:

```yaml
secrets:
  postgres:
    sealed:
      enabled: true
      encryptedData:
        postgresPassword: "<sealed string>"
        dbUri: "<sealed string>"
```

## Migration Notes

- Existing PVCs named `postgres-pvc` continue to be reused via `postgres.persistence.existingClaim`.
- The default secret name remains `atuin-postgres`; adjust `secrets.postgres.name` if you use a different name.
- The wait-for-postgres init container uses the service DNS name, which defaults to `postgres`; override `waitForPostgres.env` if you rename the service.
