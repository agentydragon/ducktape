# Agentplane external OAuth and consent probe

This is an implementation proposal, not a deployed authentication contract. Baseline:
`946231a571`, FastMCP/FastMCP-slim 3.4.4, Authlib 1.7.2. No production configuration,
credentials, or authorization behavior changes in this PR.

## Recommendation and ownership

Co-locate the OAuth authorization server with the MCP resource server in the Action Service
process, using FastMCP's `OIDCProxy` through the existing `mcp_infra` adapter. Keep its runtime
Connection/grant authority in the existing Action Service PostgreSQL database. The integration
app owns the consent and Connection-management UI and authenticates each management call through
its existing operator BFF federation. This avoids a new service, a second OAuth implementation,
and cross-database transactions between Connection authorization and Action admission.

The additional upstream Authentik authorization uses a dedicated configured OAuth client and
callback at the Action Service. It is separate from the integration app's operator-login flow.
The app's login token is never the external client's token. Compare the two verified issuer/
subject pairs through an explicit configured mapping; provider-scoped subjects are not implicitly
equal. Single-operator scope makes that mapping small, not optional.

Keep the external token's verified local issuer/client pair separate from the upstream Authentik
issuer/subject. FastMCP returns the upstream verifier's `AccessToken` after token swapping;
`DownstreamClientIdentityOIDCProxy` restores downstream `client_id` but does not rewrite its issuer
claims. Derive external issuer/client audit evidence from the verified reference token and record
upstream operator identity as separate consent evidence.

Proposed cardinality: a configured Identity can own many named Connections; a Connection has
at most one active binding; each binding revision has one authorization grant/token family.
The same OAuth registration may produce several Connections. A client name or `client_id` must
never silently merge them. Explicit reconnect replaces a Connection's old binding after a fresh
authorization. Connection names are mutable display text, with stable UUIDs as identifiers;
name uniqueness is an optional UI rule rather than an authority key.

These ownership/cardinality choices are recommendations for operator review. They are not
requirements implied by OAuth or already implemented.

## What the existing code supplies

| Reuse                                                                                | Existing source                                                                        | Limit                                                                            |
| ------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| DCR, metadata, redirect/resource validation, PKCE, callbacks, token issuance/refresh | FastMCP 3.4.4 `OAuthProxy`/`OIDCProxy`                                                 | Does not own Agentplane Connection authority                                     |
| Upstream retry classification, JWKS verification, downstream client identity         | `mcp_infra/authentik_auth/fastmcp_proxy.py`                                            | Client identity alone does not identify a Connection or grant                    |
| Shared OAuth storage                                                                 | `mcp_infra/persistence.py` `PostgresPersistence`                                       | Generic KV; not atomic application grant transitions                             |
| Verified upstream issuer/subject                                                     | `mcp_infra/authentik_auth/oidc_principal.py`                                           | Must compare against the approving operator's configured mapping                 |
| Operator login and server-side state                                                 | `x/agentplane/app/operator_sessions.py`                                                | External-client OAuth state must remain a separate aggregate                     |
| App-to-Action operator authentication                                                | `x/agentplane/app/action_federation.py`                                                | Existing adapter exposes Action methods; enrollment/management API must be added |
| Browser Origin enforcement                                                           | `x/agentplane/app/identity.py`                                                         | Per-interaction browser binding/replay protection is additional                  |
| Enrollment/grant reference implementation                                            | `haku/console/identity/fastmcp_adapter.py`, `authorization.py`, `enrollment_routes.py` | Haku's Agent, profiles and multi-operator database model do not transfer         |

The public `authorize(client, params)` method is a usable handoff hook. After calling `super()`,
the adapter can persist the already-validated `(client_id, redirect_uri, code_challenge)` and
hold the exact returned upstream URL, returning an opaque integration-app interaction URL instead.
FastMCP's underlying transaction has a 15-minute TTL. DCR itself never needs an operator page.

Custom PostgreSQL storage is credential-bearing: FastMCP uses supplied `client_storage` directly
for upstream tokens, codes and transactions. It installs `FernetEncryptionWrapper` only for its
default file storage. Choose explicit access/encryption/key handling for the custom store; reuse
the existing wrapper if application-level encryption is selected. Also supply stable signing/key
configuration across replacement and replicas. Generic KV setup creates its own table and is
distinct from versioned migrations for Connection/grant authority.

`require_authorization_consent="external"` is **not** a consent callback API. In pinned source,
`OAuthProxy.authorize` returns the upstream URL directly, and `_handle_idp_callback` performs
the stock consent-cookie check only for `True`/`"remember"`. Therefore Agentplane must own the
browser binding and one-time grant approval. Merely returning a different URL is insufficient.
The public handoff probe deliberately does not claim to implement that authority.

Haku's adapter obtains upstream principal evidence from private `_code_store`, injects opaque
grant identity through protected `_extract_upstream_claims`, and wraps token issuance/refresh/
validation to revalidate grant authority. Its explicit version guard pins FastMCP 3.4.4.
In pinned `exchange_authorization_code`, the framework consumes the code and writes upstream
tokens **before** invoking `_extract_upstream_claims`. That hook alone is too late to preserve
retryable pre-consumption principal/authority failures. This explains the earlier private read
in Haku: it is an actual ordering constraint, not a justification for copying unrelated code.
Do not copy this entire adapter. Extract only a proven common checkpoint after identifying the
minimum required hooks, or keep a small version-pinned Agentplane adapter with direct compatibility
tests. A new Authlib authorization server would replace substantial working protocol machinery
without eliminating Connection/grant bookkeeping.

References: [official OAuth proxy overview](https://gofastmcp.com/servers/auth/oauth-proxy),
[versioned upstream source](https://github.com/PrefectHQ/fastmcp/tree/v3.4.4/src/fastmcp/server/auth).
The installed 3.4.4 distribution and the hermetic probe determine compatibility; current online
documentation may describe later interfaces. Re-run this seam against pending MCP/FastMCP
migrations before landing on a new dependency version.

## Proposed application contracts

Action Service owns these records, with explicit migrations separate from FastMCP's KV table:

- `Connection`: UUID, operator-assigned display name, current binding or unbound state.
- Immutable binding revision: Connection UUID, configured Identity ID, revision, grant UUID,
  authenticated authorization-server issuer and downstream client ID, activation/end timestamps.
- Grant: bounded lifecycle and current validity, with token-family linkage managed through the
  framework adapter. Access and refresh credentials resolve the same immutable binding.
- Consent interaction: opaque handle hash, expiry, validated client presentation/correlation,
  held upstream URL, browser-binding digest, decision/version, selected Identity and Connection,
  and the approving operator's verified principal. OAuth secrets remain only in server storage.

Proposed operator API, consumed only through the authenticated integration-app BFF:

- `GET /v1/operator/connections` and `GET /v1/operator/connections/{id}`.
- `PATCH /v1/operator/connections/{id}` with expected version and display name only.
- `POST /v1/operator/connections/{id}/unbind` with expected version and idempotency key.
- Consent preview/bind and decision routes under `/v1/operator/connection-enrollments/{handle}`;
  the decision contains expected version, idempotency key, allow/deny, configured Identity, and
  create-name or explicit reconnect-Connection choice. No caller-provided callback/upstream URL.

Names are provisional. The API's main invariant is a single atomic transition from pending
interaction to one binding/grant, with retry returning that same result and a conflicting decision
returning conflict. Fresh authorization requires a fresh interaction; raw DCR creates no authority.

Browser handoff:

1. FastMCP validates authorization and persists its transaction; the adapter stores the held URL
   and returns an opaque enrollment link. Treat any client presentation as untrusted text.
2. Integration-app login retains a same-origin return destination and separate OIDC state.
   Bind the interaction to the authenticated browser using a fresh per-interaction nonce/form
   token. Persist bindings across restart; scope cookies/state by interaction for concurrent tabs.
3. An exact-Origin, operator-authenticated POST sends the choice through the BFF. Action Service
   independently verifies the operator token and atomically records the approval. A guessed handle,
   forged Identity name, stale tab, expired session, or replay cannot approve anything.
4. Release only the stored framework URL. FastMCP performs upstream authorization and callback.
   Before local code exchange/token-family activation, the adapter rechecks consent, current
   configured Identity, exact binding and upstream authenticated principal against approval.
5. Failed or interrupted issuance leaves a bounded nonactive grant. A reconnect/retry cannot
   produce a second active family for the same decision. Tokens carry an opaque grant reference;
   bearer validation and Action admission resolve current validity from canonical records.

Only the last step's exact public/private FastMCP hook needs additional adapter design. The probe
below establishes the handoff path, not principal convergence or transactional issuance recovery.

## Actions, ownership and revocation

Introduce typed authenticated principals: existing Sandbox principal or an external principal
containing configured Identity, Connection UUID, immutable binding revision, grant UUID, validated
issuer and client ID. The MCP adapter supplies this from trusted authentication; tool inputs cannot
fill it. Preserve the existing owner scope for Sandboxes. For external Actions, proposed receipt
ownership is configured Identity, while every Action snapshots exact submitting Connection/grant/
binding/client provenance. That makes sibling Connection receipt access an explicit product choice.

On submit, persist ownership and provenance in the same transaction as the idempotency result.
Duplicate retries return the original receipt and never rewrite provenance. Rename/rebind/remove
and token refresh cannot alter old audit rows. Dispatch revalidates the recorded binding before
starting an Execution; it never retargets a pending Action to the Connection's current Identity.
If dispatch has already begun, retain the existing no-retry/unknown-outcome semantics.

Proposed unbind/rebind behavior: revoke the old grant and deny new calls immediately; rebind
requires a fresh OAuth consent. Existing tokens never acquire another Identity's authority.
Pending undispatched Actions from the ended binding are refused; historical receipts remain
readable to the operator and only to a currently authorized caller under the chosen owner scope.
Revocation must gate both bearer admission and dispatch, so a stale in-memory cache cannot reopen it.

First external deployment uses human allow/deny through existing Action Decisions, with current
hard denials and safety constraints. No policy language, per-Identity autoapproval, profile graph,
SandboxPreset interpretation, or backend-account OAuth is required for this slice.

## Independently reviewable implementation slices

1. **Connection authority and provenance:** static Identity configuration, runtime tables,
   grant/binding transitions, typed principal and Action snapshot/idempotency/dispatch checks.
   Prove rename/rebind/revoke/remove/refresh races retain history and refuse stale authority.
2. **OAuth adapter:** compose the pinned FastMCP provider, shared storage and minimal consent/grant
   checkpoints; test actual HTTP DCR, metadata, PKCE, refresh, revocation and issuance recovery.
   Can develop against a test authority with the slice-1 contract.
3. **Consent and Connection UI/BFF:** login-return, authenticated API client, per-interaction CSRF/
   browser binding, name/select/deny/reconnect and list/detail/rename/unbind. Test two concurrent
   tabs, logout/login, restart, replay and identity/principal mismatch. Can develop against a
   contract fixture; do not wait for OAuth protocol code.
4. **MCP adapter and external acceptance:** expose the canonical Action catalog/submit/receipt/
   bounded-wait/cancel surface under the two authenticated caller types; prove Claude.ai real
   discovery → submission → human deny/allow → upstream result → reconnect/recovery. Local Claude
   Code is a second independent acceptance client. Configure production federation/OAuth only
   after the reviewed design and deployment choices are accepted.

## Probe and remaining decisions

`//mcp_infra/authentik_auth:test_external_consent` exercises the public authorize hook using a
real mock OIDC HTTP server and two successive FastMCP instances sharing public storage. It checks
machine DCR, validation before handoff, distinct concurrent authorization interactions, original
state/callback preservation, PKCE rejection, code replay rejection and downstream client identity.
It intentionally has no production consent routes. Shared in-memory storage proves provider
replacement compatibility, not PostgreSQL durability or simultaneous-replica atomicity.

Verification: the focused Bazel/RBE target passed in 17.4 seconds, invocation
[`db73f3a5-33f3-40ed-9a65-2f53a0587676`](https://app.buildbuddy.io/invocation/db73f3a5-33f3-40ed-9a65-2f53a0587676).
Repository hooks passed. This proves the listed hermetic behavior, not live Claude.ai onboarding.

The first run found a pinned-library error-mapping mismatch: a wrong OAuth resource is rejected
by FastMCP with `invalid_target`, but the MCP SDK's `AuthorizationErrorResponse.error` excludes
that value, so the HTTP handler returns `server_error`. No consent interaction was created.
This is a refused request with degraded diagnostics, not an admission bypass. The probe records
the current behavior; migration work should check whether the new SDK closes this mismatch.

Operator choices remaining: accept AS/runtime-store co-location; accept many Connections per
Identity; choose receipt visibility across sibling Connections; confirm rebind requires new OAuth
and ends undispatched work; choose whether Connection names must be unique. Production hostname,
Authentik client/subject mapping and deployment configuration remain a later concrete review.
