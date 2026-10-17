# Central GoProxy lab

A bounded GoProxy v1.9.2 feasibility spike, with no image, deployment, or production caller.
The existing Python central proxy remains authoritative. The runner-side Go conversion is a
separate lane.

```sh
bbr test //agentplane/egress/x/goproxy/...
bb run //agentplane/egress/x/goproxy/cmd:goproxy_lab -- \
  --snapshot=/absolute/lab-snapshot.json --ca=/absolute/interception.pem \
  --key=/absolute/interception-key.pem --roots=/absolute/upstream.pem \
  --kubeconfig=/absolute/kubeconfig --audience=agentplane-egress --namespace=lab
```

The lab binds loopback only and exempts exactly `127.0.0.1/32` for local scripted upstreams.
`--fixture-host` optionally maps a lab hostname to that address. Neither is deployment wiring.
Snapshot JSON contains the shared `PolicySet` and `PolicyBinding` shapes in `Sets` and `Bindings`,
`FreshUntil` as an RFC3339 timestamp, and fixture-only `Credentials` mapping names to `Header`
and `Value`. Keep this file private. Replacing it atomically changes the next admission.
The executable performs real TokenReview calls against the supplied API server; it does not mint
tokens or assume that an envtest server provides Kubernetes bound-token authentication.

The supported policy subset is exact hosts, exact method/path matching, and whole-value header
credentials. Unsupported wildcard/path syntax does not match. Credential CR watches, JSON/basic/
scheme/projected-token substitution, decision history, rules/admin APIs, production address pin
cache, readiness, metrics and graceful drain are deliberately outside this transport experiment.
The snapshot callback is a lab seam, not a replacement informer implementation.

Evidence, gaps and source references: [investigation](../../debug/2026_10_16_goproxy_spike.md).
