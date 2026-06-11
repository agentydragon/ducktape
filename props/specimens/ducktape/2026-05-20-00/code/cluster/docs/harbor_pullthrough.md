# Harbor Pull-Through Cache

Harbor acts as a pull-through cache for upstream registries (Docker Hub, GHCR, Quay, registry.k8s.io),
avoiding rate limits and speeding up image pulls.

## How It Works

1. **Talos registry mirrors** in `terraform/main/talos.tf` redirect containerd
   image pulls through Harbor proxy cache projects, with upstream as fallback.
2. **Harbor proxy cache projects** (public, anonymous pull) are created by tofu-controller
   via `terraform/gitops/harbor-proxy-cache/`. Each project maps to an upstream registry endpoint.
3. **Fallback ensures turnkey bootstrap** — first boot pulls from upstream (Harbor doesn't exist yet),
   subsequent pulls use cache automatically.

## Alternatives Not Selected

| Option                   | Why not                                            |
| ------------------------ | -------------------------------------------------- |
| Kyverno image mutation   | Adds dependency, no fallback, observability gap    |
| Custom admission webhook | Over-engineered, reinvents wheel                   |
| ImagePolicyWebhook       | Complex kube-apiserver config, not viable on Talos |
