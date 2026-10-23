# Agentplane staging MCP deployment

The external endpoint is `https://agentplane-actions-staging.allegedly.works/mcp`.
The integration app at `https://agentplane-staging.allegedly.works` owns operator login,
Connection management, consent, and Action review. Consent binds an external MCP client to a
labeled caller ServiceAccount; staging's `claude.ai` Connection acts as
`system:serviceaccount:agentplane-staging:claude-ai`. The Go Action core records the submitting
ServiceAccount as request identity. Connection IDs and grant history stay in ConnectionService;
they are not copied into Action request provenance. Registration alone grants no authority. See
the [Action Service caller contract](../../agentplane/action_service/SPEC.md#caller-admission-and-connection-authority)
and [ConnectionService API](../../agentplane/mcp_edge/connection_service.proto).

## Ownership and boundaries

- `tf/gitops/sso-providers/provider_agentplane_mcp.tf` owns the separate Authentik OAuth client,
  Rai-only access binding, managed-user UUID mapping, persistent token-signing and OAuth-storage
  encryption keys, and reflected `agentplane-mcp-oauth` Secret. App login and Action operator
  federation keep their existing clients and credentials. The upstream callback is exactly
  `https://agentplane-actions-staging.allegedly.works/auth/callback`, not the app login callback
  or an external client's DCR redirect URI.
- The upstream provider emits UUID subjects. The adapter checks that exact UUID and upstream
  issuer, then maps it to the same UUID under the existing Action operator issuer. It does not
  authorize by username, browser-supplied Identity, or arbitrary upstream login.
- Terraform creates the Secret in `authentik`; Reflector copies it only to `agentplane-staging`.
  The Actions process mounts its three credential files read-only. No Secret is mounted into
  the app, runner, workload proxy, or Sandbox. Required references gate pod startup; Reloader
  restarts the consumer on changes. OAuth configuration is injected as `AGENTPLANE_ACTIONS_OAUTH`.
- The Action database holds durable enrollments, Connections, grants, and OAuth state. The Go
  authorization server stores local opaque token values only as hashes; recoverable upstream
  refresh tokens remain plaintext in PostgreSQL, which the operator accepts for this rollout.
  The Go service does not use FastMCP's signing or encryption key files; keep the existing keys
  and Secret through the rollback window, then remove their fields with stage O. Preserve the
  database and Connection/grant identities across the cutover.
- The Gateway exposes exact OAuth/MCP paths only. REST, operator methods, health, and API docs
  remain off the public route. Cilium admits the Gateway's `ingress` identity on the Action port,
  plus the existing app/proxy paths; it does not admit arbitrary in-cluster clients. The Action
  service still authenticates and authorizes every protected request.
- Authentik discovery/JWKS/token traffic uses the existing TLS-SNI-bounded egress to
  `auth.allegedly.works`. No forward-auth outpost is inserted into MCP streaming. The public
  hostname uses the existing wildcard DNS record and Gateway wildcard certificate.
- Hosted Sandboxes use the generated Actions RPC/facade through egress. The proxy substitutes the
  Pod-bound workload token for the inert placeholder; the workload does not authenticate to `/mcp`
  and receives no operator or OAuth-management authority. The Go caller contract is described in
  the [Action Service specification](../../agentplane/action_service/SPEC.md#actions-mcp-frontend).

The upstream provider includes `offline_access`, which Authentik requires alongside the requested
scope to issue refresh tokens ([provider documentation](https://docs.goauthentik.io/add-secure-apps/providers/oauth2/)).
The Fernet key uses 32 cryptographically random bytes from Terraform's `random_id`, encoded as
padded URL-safe base64 ([resource documentation](https://registry.terraform.io/providers/hashicorp/random/latest/docs/resources/id)).

## Rollout gate and acceptance

Do not enable this configuration until the Action Service and migration images contain the
external OAuth adapter and enrollment/grant migrations, and the app image contains consent UI/BFF.
Keep the existing Flux image-policy ownership; verify the published revisions before merging the
activation change. A healthy old image does not establish OAuth support. No live-client acceptance
is implied by offline validation.

After the operator merges and the normal controllers reconcile:

1. Check Terraform/Reflector delivery, Flux readiness, the intended app/Actions/migration revisions,
   successful migration, and Secret references without printing Secret contents. Confirm the
   public route resolves to Actions and private REST/operator paths are not routed publicly.
2. Add the MCP URL in Claude.ai. Complete DCR, log into the integration app, name the Connection,
   choose `agentplane-staging/claude-ai`, and authorize. Confirm the upstream login returns through the Actions
   callback and then to the original client's registered callback. Denial creates no active grant.
3. Submit a harmless Action from the external client. Inspect its exact arguments and submitting
   ServiceAccount in the integration app, allow it, and recover the same request's safe result.
   Confirm exactly one Execution. Connection and grant state remain visible in ConnectionService;
   the Action request itself does not record client or grant provenance. Deny another request and
   confirm no execution.
4. External MCP clients must authorize once against Go; use the app's confirmed reconnect to
   bind the new authorization to the existing Connection. Check token refresh, restart with the
   retained database, same-key submission replay, and recovery of existing receipts. Unbind the
   Connection: old access/refresh credentials must no longer authorize new work; immutable history
   remains and already-claimed execution is not killed.
5. From a hosted Sandbox, run the CLI catalog and harmless submission/receipt flow through the
   generated Actions RPC/facade. Verify egress substitutes the workload token for the placeholder.
   It requires no DCR and must not acquire operator authority.

The app supports explicit Connection rebind and confirmed reconnect during consent. Policy editing,
upstream credentialed tool migration, and Haku retirement remain outside this rollout.
