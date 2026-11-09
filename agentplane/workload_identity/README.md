# Workload identity

Issues and resolves the SVIDs Agentplane workloads present, per
[the workload identity lifecycle](../plans/workload_identity_lifecycle.md).

- `api/v1alpha1`: the `WorkloadCredential` kind. Its owner writes `spec`; only the issuer writes
  `status`. A record's subject changes only together with a new binding (a CRD transition rule).
- `requester`: how a Go owner asks for a credential (`Requester.Ensure`, `Requester.Revoke`). It
  reads records uncached, and reports a credential only once `status.observedGeneration` has caught
  up with the spec it wrote.
- `resolver`: how a verifier turns a verified leaf into its subject. It parses
  `spiffe://agentplane/<namespace>/<name>/<credential-id>`, makes one uncached GET of the record,
  and requires the record's current credential ID and URI. A record with a controller owner or a
  binding also needs `spec.binding` to equal `status.acceptedBinding`, so an owner that clears or
  replaces the binding revokes at once, before the ID rotates. Identity subjects are refused until
  `IDENTITY_CUTOVER`.
