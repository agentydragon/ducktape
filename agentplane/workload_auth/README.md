# Workload authentication

[`go/auth`](go/auth/auth.go) provides the TokenReview resolver used by Agentplane services. Each
service constructs a `Resolver` with its accepted audience and allowed namespaces. Its namespace
allowlist is checked for every request.

`ResolveAuthorization` accepts exactly one `Bearer <token>` value. `ResolveToken` verifies the
reviewed ServiceAccount identity and returns a Pod-bound `Principal` containing the account, the
ServiceAccount UID, and the Pod name and UID. The API server's TokenReview checks the bound Pod;
accepted review results are cached for up to 30 seconds by default, so revocation or Pod deletion
may take that long to affect a cached token. Rejections and review failures are not cached.

`ResolveServiceAccountToken` returns a `ServiceAccountPrincipal` for destinations whose authority
belongs to the account alone. It requires a ServiceAccount UID and accepts either no Pod binding
claims or one complete Pod name and UID pair. Partial or repeated claims are rejected. The Action
Service uses this path. Other callers use Pod-bound `Principal` values.

[`methods.go`](go/auth/methods.go) admits a gRPC server's calls in one interceptor: `Methods` maps
each full method name to the `Rule` admitting its callers and refuses any method it does not list
([gRPC services](../docs/grpc_services.md#authentication)).

Delegated calls are a separate, opt-in mode on an authenticated direct `Rule`. `AllowDelegationFrom`
pins exact actor ServiceAccounts and per-actor caller namespace scopes to the Rule; attaching that
Rule to a full method name is the method trust grant. It preserves the Rule's ordinary direct path.
The actor token must be Pod-bound and is reviewed for this service's audience. The asserted caller
must match the live ServiceAccount UID. A method with no delegation policy rejects the delegated
scheme and any delegation envelope; `Bearer` plus an envelope is rejected rather than treated as a
direct call.

Handlers can read the typed result with `Admitted` or use `EffectiveCaller` for ordinary account
checks. `DirectAdmission` keeps the direct caller and optional Pod proof together;
`DelegatedAdmission` keeps the actor's Pod-bound `Principal`, effective ServiceAccount identity and
provenance separate. A delegated actor is never installed in the legacy `Caller` context, which
continues to mean a directly authenticated Pod-bound principal. The effective caller is account
identity only and cannot prove a Pod binding.

[`go/grpc`](go/grpc/client.go) provides `DialDelegated` and `WithDelegation`. The dialer reads the
destination-audience token file for each call, emits the distinct `Agentplane-Delegated` scheme and
binary envelope, rejects duplicate authorization/envelope metadata, and disables retries. It does
not install the ordinary Bearer interceptor. Existing direct clients continue using their current
Bearer helper.

[`audience.py`](audience.py) defines `WORKLOAD_TOKEN_AUDIENCE`, the `agentplane-egress` audience
configured for Sandbox workload tokens. Cluster configuration passes the audience to each verifier;
the Go resolver has no default audience.

Operator and browser-session authentication use separate mechanisms and do not produce a workload
principal. The approved [delegated authorization design](../docs/delegated_authorization.md) records
the shared protocol. Its Action Service sender and SessionDispatcher receiver integrations remain
separate work and are prerequisites for an operational delegated call.
