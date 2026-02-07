# Traefik Helm Chart

Helm packaging for the Traefik ingress controller running as a DaemonSet.

## Highlights

- Mirrors the existing DaemonSet-based deployment with MetalLB-assigned LoadBalancer IP.
- Creates RBAC, ServiceAccount, and default IngressClass automatically.
- Allows customization of Traefik command-line args, extra volumes, resources, and annotations via `values.yaml`.

## Usage

```bash
cd k8s/helm/traefik
helm dependency build
helm upgrade --install traefik . --namespace traefik --create-namespace
```

Adjust defaults using an override file, for example to change the advertised IP:

```yaml
service:
  annotations:
    metallb.universe.tf/loadBalancerIPs: 10.0.200.150
```

## Migration

- The legacy YAML manifests under `k8s/traefik/` are deprecated; use this chart going forward.
- Existing ingress resources use the `traefik` IngressClass; the chart recreates it by default. Set `ingressClass.create=false` if one already exists.
- Resources inherit labels from `common-lib` helpers to stay consistent with other charts.
