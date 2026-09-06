# Agentplane Action Service

This package is the first standalone ActionRequest coordinator. It owns its PostgreSQL schema and
`/v1/action-requests` lifecycle; the integration app, Haku Console, relays, and external harnesses are
clients rather than canonical state owners.

The v0 executable seam is deliberately small:

- one invariant request envelope, with caller ownership derived from the authenticated bearer;
- caller-own and operator-all reads, recursively redacting credential-shaped fields;
- one human `DecisionProvider`, with expected-version and idempotency protection;
- automatic dispatch after allow, one `Execution` row, and no retry after dispatch may have begun;
- restart recovery: pending dispatches resume, while running dispatches become `execution_unknown`;
- one explicit `agentplane:v0.echo` fixture executor proving the service boundary; and
- a durable pending-decision outbox reference containing no request arguments.

`KubernetesTokenAuthenticator` is the first identity adapter. It maps reviewed, audience-bound
ServiceAccount subjects to caller or operator roles. It does not prescribe a second per-Sandbox
Action Relay.

Managed Sandboxes already use the pod-local `egress-sidecar` and central substitution gateway. The
sidecar's projected `agentplane-egress` token is a **hop credential** carried in `Proxy-Authorization`;
it authenticates the Pod to the central gateway and is not an Action Service bearer. An Action
Service request should extend that existing path: the runner presents only a non-secret placeholder,
and the trusted gateway path may substitute a distinct, short-lived downstream token with audience
`agentplane-actions`. If a trusted component receives that optional downstream projection,
`ProjectedTokenFile` re-reads its rotating file for each service call. The runner receives receipts,
not either token. Direct KSA callers and BFFs can instead present their own reviewed
`agentplane-actions` token. External OIDC/JWT validation is a later adapter, not a different
ActionRequest API.

Migrations run separately through `:migrate`; the server verifies/uses only the migrated schema and
does not create tables at startup. `:image` and `:migration_image` are independently deployable OCI
targets.
