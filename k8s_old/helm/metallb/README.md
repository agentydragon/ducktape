# MetalLB Helm Chart

Configures MetalLB resources (IPAddressPools, L2Advertisements) for the cluster. Use alongside the upstream MetalLB deployment chart or manifests.

## Features

- Declarative IP address pool list drawn from values.
- Optional namespace resource with pod-security labels.
- Supports multiple advertisements tied to different pools.

## Usage

```bash
cd k8s/helm/metallb
helm dependency build
helm upgrade --install metallb-config . --namespace metallb-system --create-namespace
```

Override defaults in a custom values file, for example:

```yaml
pools:
  - name: main-pool
    spec:
      addresses:
        - 10.0.200.100-10.0.200.149
  - name: dmz
    spec:
      addresses:
        - 10.0.200.150-10.0.200.159
l2Advertisements:
  - name: main
    spec:
      ipAddressPools: [main-pool]
  - name: dmz
    spec:
      ipAddressPools: [dmz]
```

Set `namespace.create=true` if you want Helm to manage namespace labels:

```yaml
namespace:
  create: true
  labels:
    pod-security.kubernetes.io/enforce: baseline
```
