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

[`audience.py`](audience.py) defines `WORKLOAD_TOKEN_AUDIENCE`, the `agentplane-egress` audience
configured for Sandbox workload tokens. Cluster configuration passes the audience to each verifier;
the Go resolver has no default audience.

Operator and browser-session authentication use separate mechanisms and do not produce a workload
principal.
