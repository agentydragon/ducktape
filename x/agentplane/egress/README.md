# Agentplane egress proxy

The central proxy of the [secure egress integration](../plans/egress_proxy.md): a mitmproxy
addon that proves each caller's Pod-bound token, decides the request from `EgressPolicy` and
`EgressBinding` resources, and substitutes the real credential. What it guarantees is in
<SPEC.md>.

```sh
bbr test //x/agentplane/egress/...
```

## Layout

- `resources.py`: the boundary models of the two kinds, Sandboxes, and Secrets as read off the
  API server.
- `policy.py`: the pure decision over an in-memory `Index` — subject bindings, first matching
  rule, substitution, binding status. No I/O.
- `identity.py`: TokenReview, live Pod lookup, Sandbox owner, and the bounded verdict cache.
- `informer.py`: list-and-watch of the four kinds into the `Index`, and the binding status
  writes.
- `addon.py`: the mitmproxy addon gating CONNECTs and requests; `decisions.py` the ring and the
  JSON log line; `admin.py` the `/decisions` and `/healthz` listener.
- `proxy.py`: mitmproxy hosted in-process with the fail-closed options pinned; `main.py` the
  entry point and its `Settings` (`--flags` and `AGENTPLANE_EGRESS_*`).
- `testing/`: the fake API server and the throwaway CAs the tests run against.

## Running

The proxy needs an interception CA (`--ca-cert`, `--ca-key`) that the runner containers trust
and a writable `--confdir` where mitmproxy keeps it and the leaves it issues; upstream
certificates are verified against the image's system trust store. Identity and policy come from
the API server, in-cluster or through `--kubeconfig`.

The image `//x/agentplane/egress:image` is published as `agentplane-egress`
(<../../../devinfra/ci/image_targets.json>); the Deployment, sidecar, and CA distribution are the
cluster manifests' concern (`cluster/k8s/agentplane-staging`).

## ServiceAccount permissions

In the sandbox namespace: `get`, `list`, `watch` on `egresspolicies`, `egressbindings` and
`sandboxes.agents.x-k8s.io`; `get` on `pods`; `patch` on `egressbindings/status`. In the
credentials namespace (`--credentials-namespace`, `agentplane-egress-credentials` by default):
`get`, `list`, `watch` on `secrets`, and nothing in the sandbox namespace. Cluster-wide: `create`
on `tokenreviews.authentication.k8s.io`. The `EgressBinding` CRD must enable the `status`
subresource, which the status writes go through.
