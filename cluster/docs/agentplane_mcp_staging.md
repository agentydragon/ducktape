# Agentplane staging MCP deployment

The external endpoint is `https://agentplane-actions-staging.allegedly.works/mcp`.
The integration app at `https://agentplane-staging.allegedly.works` owns operator login,
Connection naming, Identity selection, consent, and Action review. Registration alone grants
no authority. The initial configured Identity is `personal`; external Actions require human
approval even though the credentialless workload echo fixture has a bounded auto-allow provider.
Connections sharing `personal` share Action ownership/read scope but retain distinct authenticated
client, Connection, and grant provenance.

## Ownership and boundaries

- `tf/gitops/sso-providers/provider_agentplane_mcp.tf` owns the separate Authentik OAuth client,
  Rai-only access binding, managed-user UUID mapping, persistent token-signing and OAuth-storage
  encryption keys, and reflected `agentplane-mcp-oauth` Secret. App login and Action operator
  federation keep their existing clients and credentials. The upstream callback is exactly
  `https://agentplane-actions-staging.allegedly.works/auth/callback`, not the app login callback
  or an external client's DCR redirect URI.
- The upstream provider emits UUID subjects. The adapter checks that exact UUID and upstream
  issuer, then maps it to the same UUID under the existing Action operator issuer. It does not
  authorize by username, browser-supplied Identity, or arbitrary upstream login. The separate
  staging acceptance operator does not receive access to this external MCP provider.
- Terraform creates the Secret in `authentik`; Reflector copies it only to `agentplane-staging`.
  The Actions process mounts its three credential files read-only. No Secret is mounted into
  the app, runner, workload proxy, or Sandbox. Required references gate pod startup; Reloader
  restarts the consumer on changes. OAuth configuration is injected as `AGENTPLANE_ACTIONS_OAUTH`.
- The Action database holds durable enrollments, Connections, grants, and encrypted OAuth KV
  records. Preserve database and keys together across restart/replacement. Destroy protection
  guards the Terraform key resources. Key loss/rotation requires explicit recovery and grant
  invalidation; restarting a pod is not a rotation mechanism.
- The Gateway exposes exact OAuth/MCP paths only. REST, operator methods, health, and API docs
  remain off the public route. Cilium admits the Gateway's `ingress` identity on the Action port,
  plus the existing app/proxy paths; it does not admit arbitrary in-cluster clients. The Action
  service still authenticates and authorizes every protected request.
- Authentik discovery/JWKS/token traffic uses the existing TLS-SNI-bounded egress to
  `auth.allegedly.works`. No forward-auth outpost is inserted into MCP streaming. The public
  hostname uses the existing wildcard DNS record and Gateway wildcard certificate.
- Hosted Sandboxes retain the existing workload-token substitution path. The Actions egress
  destination adds exact `/mcp` access with the existing credential reference; it does not grant
  operator or OAuth-management access or expose the real workload token to the harness.

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
   choose `personal`, and authorize. Confirm the upstream login returns through the Actions
   callback and then to the original client's registered callback. Denial creates no active grant.
3. Submit a harmless credentialless Action. Inspect exact arguments and authenticated submitting
   Identity/client/Connection in the integration app, allow it, and recover the same request's safe
   result. Confirm exactly one Execution. Deny another request and confirm no execution.
4. Check token refresh, restart with retained keys/database, same-key submission replay, and
   recovery of existing receipts. Unbind the Connection: old access/refresh credentials must no
   longer authorize new work; immutable history remains and already-claimed execution is not killed.
5. Verify the same generic MCP catalog/submission/receipt workflow from a hosted Sandbox using
   the existing placeholder token. It requires no DCR and must not acquire operator authority.

No rebind/reconnect UI, configurable per-Identity policies, upstream credentialed tool migration,
or Haku retirement is delivered by this wiring.
