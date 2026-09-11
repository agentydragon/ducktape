# Operator sessions and Action federation

## Implemented boundary

The app's existing routes remain unchanged. Its browser cookie now contains only a signed random
256-bit session handle. `operator_browser_session` in the **app database**, not the Action database,
holds Authlib's state/nonce/PKCE verifier while login is pending, then verified login issuer, stable
`sub`, display username, absolute deadline, and (only when Action federation is configured and its
lifetime is known) the access token. ID tokens and refresh tokens are not retained. Neither identity
nor OAuth/token material is encoded in the cookie. The row key is a SHA-256 digest of the handle.

The app follows its existing `TrajectoryStore.ensure_schema()` startup DDL pattern: it creates the
new table and expiry index with SQLAlchemy, under a PostgreSQL transaction advisory lock shared
by app startups. There is no separate app Alembic runner today. The Action schema is unchanged.
Existing signed-payload cookies are deliberately invalid after rollout: log in again. Replicas
must use the same app database, OIDC configuration, public origin, and session signing secret.

Login requires a verified signature, the exact configured issuer, a single audience naming the
login client (string or singleton list), a matching `azp` when present, and valid state/nonce/expiry.
Pending login expires after at most ten minutes. Authentication rotates the handle, deletes the
pending row, and consumes all OAuth state. Login lasts at most `AGENTPLANE_OIDC_SESSION_SECONDS`
(default eight hours), shortened to the verified ID-token expiry and retained access-token expiry.
There is no sliding renewal and no refresh grant: expiry requires another authorization-code login.
An access token without a known future expiry is discarded; federation then returns
`operator_reauthentication_required`. Browser login alone does not require an access token.

Each request re-reads its row. PostgreSQL row locking serializes same-session requests across
replicas through response headers (not the lifetime of an SSE stream). Callback rotation/logout
cannot be undone by an older request saving stale state. Logout deletes the entire row; cookie
replay then fails on every replica. Deleting a row also invalidates that session administratively.
Expired rows are rejected immediately and deleted on access; successful logins additionally clean
up expired rows. There is no background retention scheduler. Backups may retain expired credentials:
restrict DB/backup access accordingly. SQLAlchemy parameter logging is disabled for the app engine.
The database and its backups now contain credentials; use the existing encrypted storage and
restricted app DB role, not a read-only analytics role. No new encryption-key service is introduced.

Cookies remain HttpOnly, SameSite=Lax, and Secure with the `__Host-` name on HTTPS. Unsafe
session-authenticated requests, including logout, require an **exact** same-origin `Origin` header;
missing, trailing-slash, and foreign origins fail. Kubernetes-token callers keep their separate
non-ambient authentication and never enter the operator Action path. Responses are `no-store`. The app disables Uvicorn access logs to keep OAuth callback codes out of
request URLs in logs; callback failures use fixed messages without provider/query text.
Already-admitted requests/streams are not retrospectively cancelled by logout; revocation gates the
next request. Upstream account disablement is not polled; without a fresh login, the absolute expiry
is the browser identity lifetime. Token exchange may reject an upstream-revoked access token sooner.

## Explicit configuration required before deployment

**Staging GitOps wiring:** `tf/gitops/sso-providers/provider_agentplane_actions.tf` provisions the
Action-only Authentik target and its policy binding. The non-secret `action-federation` and
`operator-oidc` JSON is Git-owned in
`cluster/k8s/agentplane-staging/actions/configmap-action-federation.yaml`; both Deployments read it
from that ConfigMap through their existing Settings environment sources. Terraform still owns the
Authentik provider and the credential-bearing Secrets, but does not render this configuration. This
is configuration, not a credential or evidence of a successful live exchange. Never mount a shared
BFF operator bearer, forward a workload token, or invent a BFF signing authority.

The existing Haku hostexec Authentik pattern is the supported exchange shape:
`grant_type=client_credentials`, `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`,
and the current operator's login access token as `client_assertion`. No client secret is sent to the
exchange target. This is not RFC 8693 token exchange. Each request creates a fresh exchange client;
there is no mutable application-global operator-token cache.

To opt in, set the app's `action_federation` YAML key (or `AGENTPLANE_ACTION_FEDERATION` JSON) with
**all** these fields. Values below are descriptions, not deployable defaults:

- `mode`: `exchange` for the staging Authentik provider boundary; `direct` only when the login token
  already has the target issuer, audience, and subject format.
- `service_url`: canonical Action Service base URL; use a trusted internal route or HTTPS.
- `token_endpoint`: Authentik's shared HTTPS token endpoint. Loopback HTTP is accepted for tests only.
- `login_jwks_uri`: pinned JWKS URI for the configured `AGENTPLANE_OIDC_ISSUER` login provider.
- `scope`: the exact reviewed federation scope set; no proxy-outpost `ak_proxy` scope by default.
- `target.issuer`: exact per-provider issuer of the new Action-only federation target.
- `target.audience`: that target's OAuth client ID, also required as `azp`.
- `target.jwks_uri`: its pinned HTTPS JWKS URI.
  Configure the login and Action providers with the same Authentik `sub_mode`, so the exchanged
  token preserves the login token's subject. This is an identity-continuity invariant, not an
  operator authorization list.

Set the Action Service's `operator_oidc` YAML key (or `AGENTPLANE_ACTIONS_OPERATOR_OIDC` JSON) to
the same `issuer`, `audience`, and `jwks_uri` pins as the app's `target`. Authentik's target
application policy decides who can obtain the target token; the Action Service does not maintain a
second subject list. Its legacy
`operator_bearer_file` adapter is mutually exclusive and is **not** a fallback for federation.
Partial/invalid configuration fails startup. A configured app federation without OIDC login also
fails startup. Changing Authentik's target policy changes who can exchange without a configuration
rollout; existing short-lived target tokens remain valid until their normal expiry.

The Authentik target must explicitly trust only the Agentplane login provider through
`jwt_federation_providers`, use the same `sub_mode` as that provider, use a short token lifetime, and emit signed RS256 access tokens with
`iss`, `sub`, `aud`, `azp`, `iat`, and `exp`. The shared resolver enforces these pins with its existing
30-second clock-skew allowance and five-minute JWKS cache. The target application's Authentik policy
is the operator admission boundary; the Action Service only accepts a valid token issued for its
target audience. Configure network reachability for BFF-to-token/JWKS/Action and Action-to-JWKS
explicitly. No live cluster change or live-provider claim-mapping validation was performed here.

Before declaring staging acceptance, validate the real target's subject continuity and token claims
with Rai and a denied operator. Do not grant a second account merely for the test; the two-operator
identity-continuity case remains covered by signed offline fixtures. The signed mock tests prove our
request composition, verification, and audit continuity, not the deployed Authentik policy. The BFF
verifies the retained upstream access token matches the current session's **issuer and subject**,
then verifies the exchanged token has the same subject. Authentik authorizes the exchange; the Action
Service independently verifies the resulting token and records its actual target issuer/subject in
the existing Decision issuer field.

## Staging GitOps subject proof and rollout

### Authoritative subject mode (configuration proof, not live token observation)

The login and target application policy bindings are Authentik-managed (currently Rai plus the
dedicated acceptance operator). Display names never enter authorization. Both providers explicitly
use `sub_mode = "hashed_user_id"`, which makes the same Authentik user's `uid` the `sub` claim on
both sides of the exchange:

- `data.authentik_user.agentplane_operator.pk = tonumber(authentik_user.agentydragon.id)` selects
  the already-managed account by primary key. The pinned
  [Terraform provider v2026.2.0 data source](https://github.com/goauthentik/terraform-provider-authentik/blob/v2026.2.0/pkg/provider/data_source_user.go#L149)
  calls `CoreUsersRetrieve(pk)` and its `mapFromUser` exposes API `uid` and `uuid` without deriving
  either. The [resource schema](https://github.com/goauthentik/terraform-provider-authentik/blob/v2026.2.0/docs/resources/provider_oauth2.md)
  supports the selected `sub_mode` value. No lookup by username is used for authorization.
- In the cluster's pinned Authentik 2026.2.1,
  [IDToken.new](https://github.com/goauthentik/authentik/blob/version/2026.2.1/authentik/providers/oauth2/id_token.py#L108)
  assigns `hashed_user_id` to `token.user.uid` directly. Native provider federation preserves the
  same database user, so the exchanged token retains the source `sub`; no provider-specific mapping
  or UUID derivation is needed.
- [Native provider federation](https://github.com/goauthentik/authentik/blob/version/2026.2.1/authentik/providers/oauth2/views/token.py#L414)
  finds the source AccessToken only within `jwt_federation_providers`, verifies its signature,
  and assigns its database user to the grant. The pinned implementation also calls
  `__check_policy_access` with that user: the target has a matching Rai-only Authentik application
  policy binding. This corrects the earlier documentation's assertion that target policy is skipped;
  Authentik, not the Action Service, decides whether the exchange is permitted.
  `create_client_credentials_response` calls
  `IDToken.new` for that user and the **target** provider. `to_access_token` stamps target `azp`;
  `IDToken.new` stamps target audience, issuer, `iat`, and `exp`.
- `jwt_federation_providers` names only the existing Agentplane login provider; external JWT
  federation sources are explicitly empty. The one-minute target uses the existing signing
  certificate, not an invented signing key. `openid` is its only property mapping/scope.
  Confirm the deployed certificate/JWKS still uses RSA/RS256 before live acceptance; the
  application and service fail closed for another algorithm.

Terraform binds the managed Authentik users to the target application, but does not serialize their
`uid` or `uuid` into Action Service runtime configuration. No credential-bearing plan or state
output, user token, password, or signing private key is needed in review. If the policy binding lookup
fails, reconciliation must fail; there is no placeholder fallback. Account deletion/recreation changes
the identity; review it as an Authentik authorization change. Future Authentik/provider upgrades must
preserve this source contract or update and revalidate the subject mode.

### Distribution and ordering

1. The already-existing `sso-providers-tf` health check waits for the Terraform resource. The module
   creates the Authentik target, its policy binding, and credential-bearing Secrets. The
   `agentplane-action-federation` ConfigMap is a reviewed, non-secret resource in the Actions
   Kustomization; it contains only the two verification/federation JSON objects, **no target client
   secret, user list, or shared operator bearer**.
2. Action Service depends on the Terraform layer and Reflector for the Secrets it consumes. It reads
   `AGENTPLANE_ACTIONS_OPERATOR_OIDC` from the Git-owned ConfigMap; its migration init gate and
   private Action DB remain in place. The required ConfigMap reference keeps pods from starting until
   the Actions Kustomization has delivered it. Reloader rolls both consumers when the configuration
   changes.
3. The app depends on the Action Service and retains its existing database, OIDC client, and session
   signing Secret. Its `AGENTPLANE_ACTION_FEDERATION` JSON comes from the same federation ConfigMap.
   The stable ConfigMap name plus reloader annotations rolls both processes when its pins change;
   generated application settings ConfigMaps retain their Kustomize name hashes.
   No replicas are added by this migration. Runner ingestion now supports replicas through database
   leases and shared event delivery; changing deployment scale/strategy still requires upgrading
   existing sandbox runners to the independent-attachment protocol first.
4. Use app, Action Service, and migration images published from `df4a440` (#5820) or a descendant.
   The devel CI run [34167823523](https://github.com/agentydragon/ducktape/actions/runs/34167823523)
   published all three; existing Flux image markers/policies remain enabled. Do not enable this
   configuration on the previous `5686ff8` app image (it lacks persistent sessions/federation).
   Existing cookies require a new login. App startup creates `operator_browser_session` and its
   expiry index via the existing shared-Postgres advisory-lock DDL path; no separate app migration,
   DB, signing-secret rotation, or session-replica expansion is introduced.

The BFF already reaches Action on the destination Pod port 8080, and Action admits it separately
from the workload proxy. Both now reach the public Authentik origin on host/remote-node TCP 443,
restricted by `serverNames: [auth.allegedly.works]`. This is end-to-end TLS SNI enforcement, not TLS
termination or a new proxy credential. See [Cilium's SNI policy](https://docs.cilium.io/en/v1.18/security/policy/language/#limit-tls-server-name-indication-sni)
and [the cluster's Gateway identity precedent](../../../cluster/docs/cilium_network_policy.md).
The cluster enables Envoy/Gateway API and retains Cilium's default L7 proxy. A bare FQDN selector
cannot reach these node IPs with the current CIDR match mode. There is no world/all-egress rule.
DNS stays on the existing kube-dns endpoints/53; JWKS paths are pinned in each verifier, not enforced
by L7 HTTP inspection of encrypted TLS. A different Gateway/DNS/L7-proxy setup requires revalidation.

### Live acceptance after merge/reconciliation (not performed in this PR)

- Confirm the Terraform resource and both Flux layers are ready, configuration ConfigMap references
  resolved, the intended image revisions running, Action migration healthy, and app startup completed
  its session-table DDL. Inspect status, not secret payloads or a credential-bearing Terraform plan.
- Verify public discovery/JWKS issuer and RS256 metadata against the configured login and target
  pins. Verify Hubble shows the BFF token/JWKS and Action JWKS connections admitted; a TLS connection
  with a different SNI on the same gateway must be denied. Do not weaken egress if this fails.
- Log in as Rai using the existing browser Authentik session. Through the normal UI, review a pending
  non-auto-allowed fixture Action with harmless arguments, approve it, and confirm the durable Decision
  records the target issuer and the managed user's uid subject.
  Deny a second fixture and confirm no executor dispatch. The exact `echo` fixture can auto-allow:
  choose a harmless fixture outside that bounded auto-allow path, not a production side effect.
- Confirm an existing non-authorized account cannot enter Agentplane or obtain a Decision. Do not add
  a second allowed user just to validate. Wrong issuer/audience/subject, two independent operators,
  and exchanged-subject mismatch are additionally covered by signed offline tests, not simulated live
  with copied tokens. No token, code, cookie, or credential-bearing request goes into logs/review.
- Log out, confirm old-session review access fails, and repeat login after expiry/restart. A service
  restart must retain the existing session's database authority; expiry/logout must not be undone.

## Offline validation

- `//tf/gitops/sso-providers:format` and `:validate`: real `rules_tf` formatting and schema validation
  using the repo-pinned Terraform/provider mirror, backend-free.
- `//cluster/validation:test_flux_build` and `:test_cluster_integration`: full offline Flux/Kustomize
  manifest rendering and dependency/health-check validation.
- `//x/agentplane/app:test_main`: application Settings environment parsing and OIDC source coexistence.
- `//x/agentplane/action_service:test_runtime`: Settings/catalog validation and production runtime composition.
- `//x/agentplane/app:test_action_api` and `//x/agentplane/action_service:test_operator_oidc`: signed
  offline request/authorization seams, including distinct operator identities and rejected token claims.

Run through `bbr`/CI only. There is no parallel copied-literal HCL/manifest contract test: those
assertions detected edits rather than executing federation. Synthetic Settings JSON also did not
prove a real provider issued the target token. The Git-owned ConfigMap supplies both verifiers' pins;
the provider subject mode, Authentik policy, and network restrictions above remain explicit
configuration review obligations.

These checks cannot prove a real provider issued the expected subject, that the Git-owned
configuration reached both processes, or that Cilium admitted the actual TLS path. The live
acceptance above remains required even when every offline target is green.

## Failures are distinguishable and fail closed

- No browser session: 401. A workload caller asking for Action review: 403.
- Federation absent: 503 with `detail.code=operator_federation_not_configured`.
- Expired session: 401, re-login. No usable retained access token: 403 with
  `operator_reauthentication_required`.
- Token/session or source/target subject mismatch: 403 with `operator_federation_identity_mismatch`.
- Signature/issuer/audience/azp/expiry/required-claim rejection: 403 with `operator_federation_token_invalid`.
- Signing keys unusable with no HTTP failure to report: 503 with
  `operator_federation_verification_unavailable`.
- Exchange response rejected by the OAuth client or malformed: 502 with
  `operator_federation_exchange_failed`.
  These are intentionally fixed public codes; provider bodies/exceptions and tokens are not returned.
- An HTTP failure against either provider's JWKS, the token endpoint, or the Action Service re-raises
  with the upstream status (503 when no response arrived) and
  `detail={method, url, upstream_status, error_type}`; the URL is stripped of credentials, query,
  and fragment, and no body or token is echoed.

There is still one Action authority. Only Action **arguments** are exact in authenticated operator
list/detail/decision receipts. Caller/workload arguments remain recursively key-redacted; origin,
correlation, executor/provider errors and execution results retain the existing redaction policy.
A result echoing an argument does not become an operator credential-disclosure path.

## Evidence targets

- `//x/agentplane/app:test_action_api`: signed login, request-bound exchange, independent destination
  verifier, durable decisions, two app instances with distinct DB connection pools sharing PostgreSQL,
  callback on another replica, two operators with distinct subjects and no local subject mapping,
  logout replay rejection, wrong issuer/audience/expiry/subject rejection, and real MCP single dispatch.
- `//x/agentplane/app:test_auth_routes`: server-side PKCE/state, stable subject, expiry, logout and
  strict same-origin mutations, rejected signed login claims/signatures, state/nonce mismatch, handle
  rotation and callback replay, alongside the existing Kubernetes caller boundary.
- `//x/agentplane/action_service:test_operator_oidc`: actual operator API admission with signed
  valid arbitrary-subject and wrong-issuer/audience/azp/expired/missing-sub/wrong-signature tokens.
- `//x/agentplane/action_service:test_acceptance`: exact operator arguments including nested
  secret-looking values, recursive caller redaction, and unchanged redacted execution results.

The single human-authored `decision_note` is shared unchanged with caller and operator through
canonical polling/BFF projections; provider outcome reason fields remain separate. There is no
private human-note path. [Caller cancellation](../action_service/README.md#cancellation) is available
before dispatch claim; the operator/BFF surface has no cancellation override. Approval Web Push is
served by the app (`/push/*`, the service worker at `/sw.js`); only the Event & Notification Hub is
deferred.
