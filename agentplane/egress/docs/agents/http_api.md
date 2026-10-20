# Egress rules HTTP reference

`GET /v1/rules` on the Egress rules base URL (`AGENTPLANE_EGRESS_URL`, also in your platform
instructions), with `Authorization: Bearer agentplane-credential-agentplane-workload`, through the
configured proxy. `agentplane egress rules` does this and prints the same response with `--json`.

The response names your authenticated `subject` and effective `policies`. Their rules provide
`hosts`, `methods` and `paths` (each a `values` list, or `null` for any), and any `credential`'s
identity, description, exact placeholder, and presentation `targets`. A rule whose `credential` is
`null` allows access without substituting one. An empty `policies` list means no egress.
