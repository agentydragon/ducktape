# Workload identity

Issues and resolves the SVIDs Agentplane workloads present
([workload authentication](../docs/workload_authentication.md#workloadcredentials)).

- `api/v1alpha1`: the `WorkloadCredential` kind. Its owner writes `spec`; only the issuer writes
  `status`. A record's subject changes only together with a new binding (a CRD transition rule).
- `issuer`: the reconciler the Policy Controller hosts
  ([its contract](../policy_controller/SPEC.md#workload-credentials)).
- `requester`: how a Go owner asks for a credential (`Requester.Ensure`, `Requester.Revoke`). It
  tells its record from another's by its controller owner reference, or, for a record nothing owns
  (an Identity's Action box), by labels it writes and checks; it never adopts another's. It reads
  records uncached, and reports a credential only once `status.observedGeneration` has caught
  up with the spec it wrote.
- `resolver`: how a verifier turns a verified leaf into its subject
  ([the steps](../docs/workload_authentication.md#how-a-verifier-resolves-a-peer)).
