# TODO — Agentplane egress proxy

## Review where a projected token is substituted

`projectedWorkloadToken` has the sidecar carry an audience-scoped token to the central proxy in
`X-Agentplane-Workload-Token`, and the proxy substitute it after reviewing it (<SPEC.md>). That is
inconsistent with the sidecar's stated job — it "holds no credential and no TLS material"
(<sidecar.py>) — and this source makes it hold two.

The alternative is to substitute in the sidecar: it already has the token, so the token would never
leave the Pod and the proxy would need no second TokenReview per tunnel. What that costs is the
split the design rests on. Today the sidecar decides nothing and the proxy decides everything, so a
sandbox's reach is entirely a property of objects on the API server; moving substitution local
means the sidecar has to know which rule admits which credential at which target, which is the
enforcement half moving into the Pod the enforcement is against.

Neither is obviously right. Settle it before a third credential source picks a side by accident,
and fold the answer into the SPEC rather than leaving both shapes described.
