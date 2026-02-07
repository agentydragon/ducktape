# rspcache Helm Chart

This chart deploys the rspcache stack on k3s: a PostgreSQL instance, the proxy workload that fronts OpenAI-compatible APIs, and the admin dashboard with Authentik-protected ingress.

## Requirements

- Sealed Secrets controller (for the database secret rendered via `common.sealedSecret`).
- An existing secret named `rspcache-openai` in the target namespace that carries the upstream OpenAI credentials (`OPENAI_API_KEY`, `ADGN_OPENAI_KEYS`). The chart will reference it but does not create it.
- Traefik/Authentik middleware configured to honour the `authentik-forwardauth` annotation if ingress is enabled (default).

## Usage

```bash
cd k8s/helm/rspcache
helm dependency build        # vendor common-lib
helm upgrade --install rspcache . \
  --namespace rspcache \
  --create-namespace \
  --wait
```

The default values mirror the previous Kustomize deployment: Postgres with a 10 Gi PVC, two proxy replicas, and a single admin replica fronted by `rspcache-admin.k3s.agentydragon.com`.

## Configuration

Key `values.yaml` sections:

- `appImage`: container image shared by proxy and admin components.
- `config.requireApiKey`: toggles the `RSPCACHE_REQUIRE_API_KEY` flag consumed by both deployments.
- `sealedSecrets.db`: ciphertext for the Postgres credentials (`rspcache-db` secret).
- `postgres`: container image, resources, and PVC sizing for the StatefulSet.
- `proxy` / `admin`: replica counts, probes, resource requests, and service definitions.
- `admin.ingress`: host/TLS/annotation settings for the dashboard ingress.

Override any value set using the standard `-f` or `--set` flags.

## Cleanup

```bash
helm uninstall rspcache -n rspcache
kubectl delete namespace rspcache
```

Keep in mind that uninstalling the chart leaves the PVC intact; remove it manually if you need a fresh database.
