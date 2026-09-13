# k8s manifests — agent instructions

## Network troubleshooting: always check CiliumNetworkPolicies

CiliumNetworkPolicies in this cluster restrict some traffic.
If pods can't talk to each other (memberlist won't form, gRPC clients timeout, intra-app probes fail, ...)
check CiliumNetworkPolicies - they may need widening for required traffic.

## Authoring egress policy through Cilium Gateway

Account for the complete request path, not just the Gateway's frontend address.
For Pod traffic through a Gateway Service, Cilium can check the source Pod's
egress policy against the selected backend identity and target port. Allowing
node:443 alone does not grant that backend permission.

Inspect the HTTPRoute and backend Service before writing the policy. Select the
required backend Pods and target port explicitly, retaining the client's SNI
restriction where applicable. Also check that the backend admits Gateway's
`reserved:ingress` identity; see <../docs/cilium_network_policy.md>.

Validate a complete HTTP request through the intended Gateway path, not only a
TLS handshake. For SNI-restricted access, verify that wrong SNI and direct
plaintext backend requests are rejected. A missing backend permission can yield
an HTTP 403 after successful TLS; distinguish that from a transport failure.

Mechanism and verification recipe: <../docs/cilium_network_policy.md> § Egress
through the Gateway Service.

## Persistent SeaweedFS bucket ownership

SeaweedFS `Bucket` resources manage external buckets whose data lifecycle is
independent of the Kubernetes object. A `reclaimPolicy: Retain` deletion removes
the `Bucket` CR while leaving the physical bucket behind; recreating the CR under
a different Flux Kustomization then gives the operator a new, uninitialized
resource that it may refuse with `BucketAlreadyExists` rather than adopting the
existing bucket.

Do not move a persistent `Bucket` CR between Flux Kustomizations in a prune/delete
migration without a tested ownership handoff. Preserve the existing CR ownership,
or use an operator version and explicit adoption mechanism that supports the
handoff. Deleting the physical bucket is a destructive recovery action: verify
the backup contents and obtain explicit approval before doing it.
