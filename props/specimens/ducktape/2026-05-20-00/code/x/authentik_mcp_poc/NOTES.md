# Architecture notes

Deep-dive findings from bringing the POC up end-to-end against the live cluster.
The README describes the 50000-foot architecture; this file captures the
non-obvious mechanics that only surfaced once things were actually running.
**Three false starts before the POC worked end-to-end.** §2-§3 cover the
first two (raw Authorization header, then the swapped upstream token that
still doesn't match what the outpost wants), §4-§5 are the correct
token-exchange shape, and §6 is the third — an Authentik scope-gating
gotcha that produced a 200 with empty identity headers.

## 1. The POC's original premise

```
claude.ai ─OAuth 2.1+PKCE─▶ MCP server (OIDCProxy wraps Authentik OAuth2 provider)
  user JWT ───▶  MCP server validates ─▶ tool runs
                                       └─▶ forwards user JWT as Bearer to backend
                                               ↓
                                     Authentik proxy provider outpost
                                     (jwt_federation_providers = [mcp_poc OAuth2])
                                               ↓
                                     backend: trusts X-Authentik-* headers the
                                     outpost injects after successful federation
```

The idea: a single user-issued Authentik JWT traverses **two independent
Authentik providers** — once when the MCP server's `JWTVerifier` validates it,
once more when the proxy outpost validates it via "JWT federation". The outpost
forwards to the backend with the standard
`X-Authentik-{Username,Email,Groups,Uid}` headers.

**This mental model is wrong in two places.** Both are fixable, but getting
there required reading both FastMCP's and Authentik's source.

## 2. First wrong turn — raw Authorization header is the FastMCP JTI reference

`OIDCProxy` (a subclass of `OAuthProxy`) does NOT pass the upstream Authentik
token through to the MCP client. Concretely, from reading the wheel source
(fastmcp 3.1.0):

`fastmcp/server/auth/oauth_proxy/proxy.py` lines 950-985, after the upstream
token exchange with Authentik completes:

```python
# Store encrypted upstream token under an opaque id
upstream_token_set = UpstreamTokenSet(
    upstream_token_id=upstream_token_id,
    access_token=idp_tokens["access_token"],      # ← real Authentik JWT
    refresh_token=idp_tokens.get("refresh_token"),
    scope=idp_tokens.get("scope"),
    expires_at=expires_at,
    raw_token_data=idp_tokens,
)
await self._upstream_token_store.put(
    key=upstream_token_id,
    value=upstream_token_set,
    ttl=max(refresh_expires_in or 0, expires_in, 1),
)

# Issue minimal FastMCP access token (just a reference via JTI)
fastmcp_access_token = self.jwt_issuer.issue_access_token(
    client_id=client.client_id,
    scopes=authorization_code.scopes,
    jti=access_jti,
    expires_in=expires_in,
    upstream_claims=upstream_claims,
)
```

The token returned to claude.ai is:

- **Signed by FastMCP's `JWTIssuer`**, whose key is derived from the upstream
  client_secret via `derive_jwt_key` with salt `"fastmcp-storage-encryption-key"`.
- **A short-lived JTI reference** — it carries `jti`, `client_id`, `scopes`,
  and an `upstream_claims` blob, but it does **not** carry the upstream
  Authentik access token itself.
- **Not verifiable by Authentik's JWKS**. The signing key is different and the
  `iss` claim is the MCP server's `base_url`.

Our first `_extract_bearer_token` implementation read the raw `Authorization`
header off `get_http_request()` and forwarded that. We were forwarding the
FastMCP JTI reference, which Authentik has never heard of. That's why the
outpost returned 401 "Unauthenticated — Due to 'Receive header authentication'
being set, no redirect is performed".

## 3. Second wrong turn — the upstream token isn't what the outpost wants either

`OAuthProxy.load_access_token` runs on the MCP server side when a tool request
arrives (same file, lines 1384-1454):

```python
async def load_access_token(self, token: str) -> AccessToken | None:
    # 1. Verify the FastMCP JWT we issued
    payload = self.jwt_issuer.verify_token(token)
    jti = payload["jti"]
    # 2. Look up the upstream token via the JTI mapping
    jti_mapping = await self._jti_mapping_store.get(key=jti)
    upstream_token_set = await self._upstream_token_store.get(
        key=jti_mapping.upstream_token_id
    )
    # 3. Validate the upstream token against the upstream IdP's JWKS
    verification_token = self._get_verification_token(upstream_token_set)
    validated = await self._token_validator.verify_token(verification_token)
    # 4. Ensure the returned AccessToken carries the upstream access token
    if verification_token != upstream_token_set.access_token:
        validated = validated.model_copy(
            update={"token": upstream_token_set.access_token, ...}
        )
    return validated
```

In `JWTVerifier.verify_token` (the `_token_validator` OIDCProxy uses),
`providers/jwt.py` line 475:

```python
return AccessToken(
    token=token,     # ← whatever was passed in — i.e. upstream_token_set.access_token
    client_id=str(client_id),
    scopes=scopes, ...
)
```

So `get_access_token().token` in the tool handler IS the real Authentik JWT,
issued by our `authentik-mcp-poc` OAuth2 provider (provider 54 in the live
cluster). Good. That was our second fix. **Still got 401 "token is not active"
from the outpost at 2026-04-13T18:30:57Z.**

Here's what's actually happening inside Authentik when the outpost receives
our forwarded Bearer header. The embedded outpost validates incoming Bearer
tokens by calling RFC 7662 introspection against the **proxy provider's own**
token endpoint, authenticating as **itself** (using the proxy provider's
auto-generated `client_id`/`client_secret`). Then
`authentik/providers/oauth2/views/introspection.py:45-50` runs:

```python
access_token = AccessToken.objects.filter(
    token=raw_token,
    provider=provider,   # ← bound to whoever authenticated the /introspect/ call
).first()
```

Where `provider = authenticate_provider(request)` = the **proxy provider**
(provider 55, `authentik-mcp-poc-backend`). The filter hard-codes `provider` —
there is **no fallback**, no "also check `jwt_federation_providers`". So
Authentik looks for `AccessToken.filter(token=<our_user_jwt>, provider=55)` and
finds nothing, because our user's token was issued by provider 54. The
introspection endpoint returns `{active: false}`, the outpost logs `token is
not active`, falls through to the "unauthenticated header auth" branch, and
returns the 401 HTML page.

`jwt_federation_providers` is in Authentik source in exactly one code path:
`authentik/providers/oauth2/views/token.py::__post_init_client_credentials_jwt`,
called from the `/application/o/token/` endpoint when the grant type is
`client_credentials` and the request carries a `client_assertion_type` of
`urn:ietf:params:oauth:client-assertion-type:jwt-bearer`. It is **not** used
for forward-auth Bearer validation at the outpost. **The POC's original
premise ("the outpost federates JWTs via `jwt_federation_providers`") is
wrong** — that's not what the field does.

(Also, contrary to what this file claimed earlier, Authentik 2026.2.1 does
**not** implement RFC 8693 token exchange. `authentik/common/oauth/constants.py`
lists the supported grants and `urn:ietf:params:oauth:grant-type:token-exchange`
is not there. I'd written that from faulty memory. Removed.)

## 4. But `jwt_federation_providers` IS usable — just on the token endpoint

The field IS the right knob, just in a different place. Reading
`authentik/providers/oauth2/views/token.py` lines 414-442:

```python
def __validate_jwt_from_provider(
    self, assertion: str
) -> tuple[dict, OAuth2Provider] | tuple[None, None]:
    token = provider = _key = None
    federated_token = AccessToken.objects.filter(
        token=assertion, provider__in=self.provider.jwt_federation_providers.all()
    ).first()
    if federated_token:
        _key, _alg = federated_token.provider.jwt_key
        try:
            token = decode(
                assertion, _key.public_key(),
                algorithms=[_alg],
                options={"verify_aud": False},
            )
            provider = federated_token.provider
            self.user = federated_token.user
        except ...
    return token, provider
```

And the routing to it, lines 316-334:

```python
def __post_init_client_credentials(self, request: HttpRequest):
    # client_credentials flow with client assertion
    if request.POST.get(CLIENT_ASSERTION_TYPE, "") != "":
        return self.__post_init_client_credentials_jwt(request)
    ...
```

So if we POST to `/application/o/token/` with:

```
grant_type              = client_credentials
client_id               = <backend proxy provider's auto-generated client_id>
client_assertion_type   = urn:ietf:params:oauth:client-assertion-type:jwt-bearer
client_assertion        = <user's upstream Authentik JWT (issued by provider 54)>
scope                   = openid
```

then:

1. `TokenView.post` looks up `self.provider` by the `client_id` → provider 55
   (the backend proxy provider).
2. The confidential-client secret check at line 167 is guarded by
   `grant_type in [AUTHORIZATION_CODE, REFRESH_TOKEN]`, so `client_credentials`
   bypasses it — **no `client_secret` is required**, the JWT assertion is the
   authentication.
3. `__post_init_client_credentials` sees `CLIENT_ASSERTION_TYPE != ""` and
   routes to `__post_init_client_credentials_jwt`.
4. `__validate_jwt_from_provider` looks up the assertion in `AccessToken`
   filtered to `self.provider.jwt_federation_providers` =
   `provider_55.jwt_federation_providers.all()` = `[provider_54]`. The user's
   upstream token IS in that table (it's the token the
   `authorization_code`-grant issued), so the lookup succeeds. Authentik
   validates the signature against provider 54's JWK and sets
   `self.user = federated_token.user` — the identity is preserved.
5. Token issuance proceeds, minting a **new** `AccessToken` row with
   `provider=provider_55` and `user=<original_user>`.

That new token is exactly the shape the outpost's introspection expects:
`AccessToken.filter(token=<new_token>, provider=provider_55)` finds it,
returns `{active: true}`, and the outpost rewrites to `internal_host` with the
standard `X-Authentik-*` identity headers set from the federated user.

## 5. The fix: tool-side token exchange before calling the backend

Two-hop instead of one:

```
get_access_token().token                            → user's upstream token
        │                                             (issued by provider 54)
        ▼
POST /application/o/token/
    grant_type=client_credentials
    client_id=<provider_55's client_id>
    client_assertion_type=...:jwt-bearer
    client_assertion=<user's upstream token>
        │
        ▼                                             → backend-scoped token
access_token in response                              (issued by provider 55)
        │                                             identity = same user
        ▼
GET https://authentik-mcp-poc-backend.allegedly.works/whoami
    Authorization: Bearer <backend-scoped token>
        │
        ▼
Authentik outpost introspects → AccessToken.filter(
    token=<backend-scoped>, provider=provider_55) → MATCH → active=True
        │
        ▼
Rewrites to internal_host with X-Authentik-{Username,Email,Uid,Groups}
```

Code shape in `server.py`:

```python
async def _exchange_token_for_backend(
    user_token: str, settings: ServerSettings, client: httpx.AsyncClient,
) -> str:
    response = await client.post(
        settings.authentik_token_endpoint(),
        data={
            "grant_type": "client_credentials",
            "client_id": settings.backend_oidc_client_id,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": user_token,
            "scope": "openid",
        },
    )
    response.raise_for_status()
    return response.json()["access_token"]
```

and the tool becomes:

```python
user_token = _extract_bearer_token()
async with httpx.AsyncClient(timeout=10.0) as client:
    backend_token = await _exchange_token_for_backend(user_token, settings, client)
    response = await client.get(f"{backend_url}/whoami",
                                headers={"Authorization": f"Bearer {backend_token}"})
```

Terraform still needs `jwt_federation_providers = [authentik_provider_oauth2.mcp_poc.id]`
on the proxy provider — that's the whitelist `__validate_jwt_from_provider`
filters on. We additionally expose the proxy provider's auto-generated
`client_id` in the K8s secret so the server Deployment can read it.

## 6. Third wrong turn — scope-gating swallows the identity claims

First post-fix run of `whoami_via_backend` returned:

```json
{
  "backend_status": 200,
  "backend_response": {
    "user": "",
    "email": "",
    "uid": "6bdf979edce7a934bf8a246005bcb38103fc12b9b682b11b1dcf46cbe4402b00",
    "groups": [],
    "secret_message": "auth flowed through the Authentik proxy outpost"
  }
}
```

200 with `secret_message` set — so the outpost accepted the exchanged token
and let us through — but `user`/`email`/`groups` came back empty. Only
`uid` (which is just the stringified `sub` claim = `hashed_user_id`) was
populated.

Cause: **Authentik's property mappings are scope-gated.** Each
`PropertyMapping` is attached to a `Scope` (`openid`, `email`, `profile`,
`entitlements`, `ak_proxy`, …), and a mapping only fires when its scope
appears in the `scope=` parameter of the `/token/` request. Our first
cut of `_exchange_token_for_backend` requested `scope=openid`, so only
the OIDC minimal claims ended up in the minted backend-scoped token.

Provider 55 (`authentik-mcp-poc-backend`) has five mappings:

| scope          | name                                                      |
| -------------- | --------------------------------------------------------- |
| `openid`       | authentik default OAuth Mapping: OpenID 'openid'          |
| `email`        | authentik default OAuth Mapping: OpenID 'email'           |
| `profile`      | authentik default OAuth Mapping: OpenID 'profile'         |
| `entitlements` | authentik default OAuth Mapping: Application Entitlements |
| `ak_proxy`     | authentik default OAuth Mapping: Proxy outpost            |

The last one is the interesting one: `ak_proxy` is the scope whose
mapping populates the claim set the proxy outpost reads on the
forward-auth hop. Without it the outpost still validates the token via
introspection (success), still injects the `X-Authentik-*` headers, but
with empty values because the claims it would have read aren't there.
Without `email` + `profile` the standard `name`/`email`/`preferred_username`
claims are also missing, so the username and email fields stay blank.

You can enumerate the mappings on any provider via the API:

```
GET /api/v3/propertymappings/provider/scope/?pm_uuid=<UUID>&pm_uuid=<UUID>&...
```

passing each UUID from the provider's `property_mappings` list.

**Fix**: request every scope whose mapping you want to fire. For the POC
that's `openid email profile ak_proxy`. Don't be clever about omitting
scopes — in Authentik, a scope in the request is necessary but not
sufficient (the property mapping must be attached to the provider); a
scope attached to the provider is also necessary but not sufficient
(the request must ask for it). Both are required.

## 7. Things worth double-checking once this lands

- **Token lifetime.** Authentik's OAuth2 provider 54 has
  `access_token_validity = "minutes=10"` by default; the upstream token
  we forward must still be live when we do the exchange (which is fine
  in practice — FastMCP's refresh flow aligns its token TTL with the
  upstream expiry, so claude.ai refreshes before the underlying token
  expires). Provider 55's `access_token_validity = "hours=24"` is the
  TTL of the backend-scoped token, which only needs to outlive one HTTP
  call.
- **Policy bindings.** The backend application (`authentik-mcp-poc-backend`)
  has a policy binding to the `authentik Admins` group.
  `__validate_jwt_from_provider` **does not** run `__check_policy_access`
  — policy enforcement happens when the outpost serves the backend's
  `external_host`, not at the token exchange. So the token exchange
  succeeds for any user whose upstream token passes provider 54's
  policy; the backend-side policy check on provider 55 happens when the
  outpost processes the subsequent `/whoami` request.
- **Scope handling.** Already bit us once (§6). Request every scope whose
  property mapping you want to fire:
  `scope=openid email profile ak_proxy` for the POC. Mappings that aren't
  requested don't execute, and claims that aren't produced become empty
  `X-Authentik-*` headers on the outpost's forward-auth hop — a 200 with
  no identity, which is the confusing failure mode to watch for.
- **Not forwarding the token to multiple backends.** If we later want to
  call N backends in one tool call, each needs its own exchange keyed by
  that backend's `client_id`. There's no audience-bound reuse — each
  backend's outpost checks `provider=<its_own_provider>` in introspection.

## 8. References

- **fastmcp 3.1.0 wheel**, `fastmcp/server/auth/oauth_proxy/proxy.py`:
  - `OAuthProxy.load_access_token` token-swap pattern — lines 1384-1454.
  - Upstream token storage on /token handler — lines 895-985.
  - Refresh flow (aligns FastMCP TTL with upstream) — lines 1150-1200.
- **fastmcp 3.1.0 wheel**, `fastmcp/server/auth/providers/jwt.py`:
  - `JWTVerifier.load_access_token` returns `AccessToken(token=token, ...)` —
    line 475. (This is why the OAuthProxy swap's `model_copy` check at line
    1436 is effectively a no-op in the happy path: the inner verifier has
    already set `.token` to `verification_token`, which equals
    `upstream_token_set.access_token`.)
- **Authentik 2026.2.1**, `authentik/providers/oauth2/views/introspection.py`:
  - `TokenIntrospectionParams.from_request` — lines 41-55. The hard-coded
    `provider=provider` filter that blocks cross-provider token recognition.
- **Authentik 2026.2.1**, `authentik/providers/oauth2/views/token.py`:
  - `__validate_jwt_from_provider` — lines 414-442. The one place
    `jwt_federation_providers` is consulted.
  - `__post_init_client_credentials` router — lines 316-334.
  - `__post_init__` confidential-client secret check — lines 165-174 (only
    guards `authorization_code` / `refresh_token`, not `client_credentials`).
- **Authentik 2026.2.1**, `authentik/common/oauth/constants.py`:
  - Full list of supported grant types. Notably absent:
    `urn:ietf:params:oauth:grant-type:token-exchange` — Authentik does not
    implement RFC 8693.
