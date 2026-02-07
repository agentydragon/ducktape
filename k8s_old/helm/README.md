# Helm Charts

This directory houses all Helm packaging for Ducktape workloads. The goal is to have every Kubernetes workload described as a chart so installs and upgrades are uniform.

## Chart Standards

- **Structure**
  - Every workload gets its own chart directory with `Chart.yaml`, `values.yaml`, `values.schema.json` (optional) and a `templates/` tree.
  - Reusable helpers live in the `common-lib` library chart; individual charts should depend on it and invoke helpers such as `common.labels` and `common.blueprintLabels`.
- **Values Schema**
  - Reserve top-level keys for high-signal toggles: `image`, `service`, `ingress`, `resources`, `postgres`, `secrets`.
  - Cluster-specific overrides belong in `values/<cluster>.yaml`; keep defaults production-safe.
- **Templating Conventions**
  - Apply `{{ include "common.labels" . }}` to metadata labels and `{{ include "common.selectorLabels" . }}` for pod selectors.
  - Config file blobs come from `files/` using `Files.Get` helpers.
  - Secrets should be represented either as SealedSecret templates or external references (never plain secrets in defaults).
- **Testing**
  - Run `helm lint` and `helm template --debug --values values/<cluster>.yaml` before committing changes.

## Shared Library (`common-lib/`)

`common-lib` is a Helm library chart that publishes reusable helpers for naming, labels, and other shared resources. To use it, add a dependency in your chart’s `Chart.yaml`:

```yaml
dependencies:
  - name: common-lib
    version: ^0.1.1
    repository: \"file://../common-lib\"
```

Then call helpers from templates, for example:

```yaml
metadata:
  labels:
{{ include \"common.labels\" . | indent 4 }}
```

Additional shared templates (e.g., Postgres StatefulSets, sealed secret scaffolds) will be added here as services are migrated.

## Existing Charts

### `authentik/`

Authentik deployment along with blueprints and supporting services.

### `grafana-operator/`

Deploys the main Grafana instance using the official Grafana chart as a dependency. Configuration lives in `values.yaml` (datasources, ingress, admin user, dashboards).

### `gitea/`

Wraps the upstream Gitea chart with Authentik OAuth bootstrap jobs, sealed secrets, and reflector deployment for secret reflection.

### `rspcache/`

Deploys the rspcache proxy, admin dashboard, and backing PostgreSQL database plus required secrets/config.

### `matrix-stack/`

Umbrella chart for matrix-synapse and related services.

### `registry/`

Single-node Docker registry with optional external LoadBalancer and TLS ingress.

### `traefik/`

DaemonSet-based Traefik ingress controller with RBAC, MetalLB LoadBalancer, and IngressClass setup.

### `metallb/`

Configures MetalLB IP address pools and L2 advertisements.

### `cert-manager`

Managed via Helmfile (`k8s/helmfile/helmfile.yaml`) using the upstream `jetstack/cert-manager` chart. Bootstrap resources (self-signed issuer, CA certificate, homelab issuer) are packaged in `cert-manager-bootstrap/` and deployed as a separate release.

### `cert-manager-bootstrap/`

Applies the homelab CA certificate and cluster issuers that sit on top of the upstream cert-manager installation.

### `observability/base/`

Creates the `observability` namespace (labels configurable via values).

### `observability/timescaledb/`

Provision TimescaleDB StatefulSet, service, and sealed secret used by observability workloads.

### `observability/`

Umbrella chart that installs the namespace, TimescaleDB, and Grafana components together.

## Deployment Flow

General commands:

```bash
helm dependency update            # refresh common-lib or other deps
helm lint                         # validate templating
helm template --values values/k3s.yaml .
helm upgrade --install <release> . --namespace <ns>
```

Refer to each chart’s README for workload-specific notes (extra dependencies, job ordering, etc.).
