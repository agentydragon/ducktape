# Agentplane staging MCP deployment

The public MCP origin is `https://agentplane-actions-staging.allegedly.works`; the standalone MCP
edge serves `/mcp` and external OAuth there. The integration app at
`https://agentplane-staging.allegedly.works` owns operator login, Connection management, consent,
and Action review. The edge owns external Connections, grants, OAuth tokens, and ConnectionService.
Consent binds an external client to a labeled caller ServiceAccount; staging's `claude.ai`
Connection acts as `system:serviceaccount:agentplane-staging:claude-ai`. Action Service records
that ServiceAccount as request identity and retains upstream MCP linkage and CIMD. This describes
the declared staging deployment contract.

## Ownership and boundaries

- `tf/gitops/sso-providers/provider_agentplane_mcp.tf` owns the separate Authentik OAuth client,
  Rai-only access binding, managed-user UUID mapping, persistent token-signing and OAuth-storage
  encryption keys, and the reflected `agentplane-mcp-oauth` Secret. App login and Action operator
  federation keep their existing clients and credentials. The provider callback is exactly
  `https://agentplane-actions-staging.allegedly.works/auth/callback`, not the app login callback or
  an external client's DCR redirect URI.
- The edge OAuth adapter checks the upstream issuer and UUID subject, then maps that identity to
  the same UUID under the existing Action operator issuer. It does not authorize by username,
  browser-supplied identity, or arbitrary upstream login.
- Terraform creates the Secret in `authentik`; Reflector copies it only to `agentplane-staging`.
  The MCP edge consumes its OAuth configuration and mounts the client secret read-only. The
  Action Service has no external OAuth configuration or credential mount. Reloader restarts the
  edge when the Secret changes.
- The MCP edge database owns Connection, grant, enrollment, and external OAuth state. The edge
  stores local opaque token values only as hashes; recoverable upstream refresh tokens remain
  plaintext in PostgreSQL, which the operator accepts for this rollout. Legacy Action Service
  tables remain preserved until a separately verified data handoff and retention decision.
- The Gateway exposes only exact OAuth/MCP paths publicly. REST, operator methods, health, and API
  docs remain off the public route. Cilium admits the Gateway's `ingress` identity to the edge;
  it does not admit arbitrary in-cluster clients. The edge authenticates the external bearer and
  forwards requests under the currently bound ServiceAccount identity, which Action Service
  independently validates.
- Authentik discovery/JWKS/token traffic uses the existing TLS-SNI-bounded egress to
  `auth.allegedly.works`. No forward-auth outpost is inserted into MCP streaming. The public
  hostname uses the existing wildcard DNS record and Gateway wildcard certificate.
- Hosted Sandboxes use the generated Actions RPC/facade through egress. The proxy substitutes the
  Pod-bound workload token for the inert placeholder; a workload does not authenticate to `/mcp`
  and receives no operator or OAuth-management authority. See the [Action Service caller
  contract](../../agentplane/action_service/SPEC.md#caller-admission-and-connection-authority).

The upstream provider includes `offline_access`, which Authentik requires alongside the requested
scope to issue refresh tokens ([provider documentation](https://docs.goauthentik.io/add-secure-apps/providers/oauth2/)).
The Fernet key uses 32 cryptographically random bytes from Terraform's `random_id`, encoded as
padded URL-safe base64 ([resource documentation](https://registry.terraform.io/providers/hashicorp/random/latest/docs/resources/id)).

## Action Service configuration rollout

The generated Action Service settings retain `integrated_connection_service: false` and
`max_wait_seconds: 180` while older pinned images may still parse those keys. The new Action Service
rejects `integrated_connection_service: true`, defaults the field to false, and registers no
integrated MCP or Connection endpoints. Its `max_wait_seconds` is only a compatibility tombstone,
capped at the generated 180-second maximum;
the MCP edge owns external caller wait limits. Keep `direct_wait_seconds` because Action Service
orchestration uses it.

Remove the two compatibility keys from generated manifests only after the image containing the
false-only/no-op parser is observed running. Deploy that config removal before removing the parser
fields in a later image. This sequence preserves compatibility with an older image during rollout.
Serving-path and config rollouts do not implicitly move or delete database rows.

## Acceptance after deployment

Use these deployment acceptance checks after the image and route changes reconcile:

1. Check Terraform/Reflector delivery, Flux readiness, the intended app, Actions, edge and migrator
   revisions, successful migration, and Secret references without printing Secret contents. Confirm
   the public route resolves to the edge and private REST/operator paths are not routed publicly.
2. Add the MCP URL in Claude.ai. Complete DCR, log into the integration app, name the Connection,
   choose `agentplane-staging/claude-ai`, and authorize. Confirm upstream login returns through the
   edge's `/auth/callback` and then to the client's registered callback. Denial creates no active
   grant.
3. Submit a harmless Action from the external client. Inspect its arguments and submitting
   ServiceAccount in the integration app, allow it, and recover the same request's safe result.
   Confirm exactly one Execution. Connection and grant state remain visible in ConnectionService;
   Action request provenance records the account only. Deny another request and confirm no execution.
4. Test token refresh, edge restart with the retained database, same-key submission replay, and
   recovery of existing receipts. Unbind the Connection and confirm old access/refresh credentials
   no longer authorize new work; immutable history remains and already-claimed execution is not
   killed.
5. From a hosted Sandbox, run the CLI catalog and harmless submission/receipt flow through the
   generated Actions RPC/facade. Verify egress substitutes the workload token for the placeholder.
   This requires no DCR and must not acquire operator authority.

The app supports explicit Connection rebind and confirmed reconnect during consent. Policy editing,
upstream credentialed tool migration, and Haku retirement remain outside this deployment check.
