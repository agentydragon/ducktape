# Haku HTTP egress

This is the current contract for the colocated Haku HTTP egress proxy and its
Console decision endpoint. Research, rejected alternatives, and the design
history live in <../../cluster/docs/plans/agent_egress_proxy_options.md>.

## Scope

The egress proxy is a shared, colocated sidecar for Console-launched Agent
sandboxes. It is the enforcement point for outbound HTTP(S): Console decides
whether each connection or request is allowed and which inert placeholders may
be replaced with Console-owned credentials.

The proxy and Console are one same-release unit. The decision endpoint is
bound to Console pod localhost at:

```text
POST /api/internal/http/decide
```

No sandbox workload can route to that endpoint. The proxy has no policy copy and
does not cache dynamic decisions.

## Authentication split

The two credentials in this path have different principals and must not be
combined:

| Credential                     | Direction                               | Meaning                                                                                                                             |
| ------------------------------ | --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `HAKU_EGRESS_FENCE_CREDENTIAL` | proxy → Console, `Authorization` header | Authenticates the shared egress fence to the decision endpoint. It is endpoint-scoped and does not identify an Agent.               |
| `HAKU_SESSION_TOKEN`           | Sandbox runner → proxy and Console      | The per-Session session token. It is the sole source of Agent and session identity for HTTP egress and is also used by Console MCP. |

The decision service resolves the session token through the live
`AgentBearerAuthority`. A missing, invalid, expired, or non-session token is
denied. There is no static Agent identity fallback.

The shared fence credential is deliberately not a general Agent, MCP, session,
or operator credential. In particular, changing or presenting the shared fence
credential cannot select another Agent.

## Sandbox and runner boundary

At session allocation, Console places the random session token literally in
the runtime `SandboxClaim.spec.env` as `HAKU_SESSION_TOKEN` (and, until every
deployed runner image reads that name, as the pre-rename `HAKU_RUNNER_TOKEN` —
the `CLEANUP` in `haku/console/session/sandbox_claims.py` names the removal
condition). The upstream `EnvVar` API does not yet
support Secret-backed injection. This is safe only while runtime claims are not
broadly readable: ordinary Agents and service accounts must not get, list, or
watch claims. Console's narrow claim access and the cleanup/controller paths
are the intended readers.

Reading a runtime claim is therefore bearer disclosure, not ordinary sandbox
inspection. The token must stay out of launch frames, argv, logs, and persisted
session records; only an audit-safe fingerprint may be recorded.

The runner inherits the claim-owned token. When Console selects an
`HTTP_PROXY` or `HTTPS_PROXY`, the runner puts the same token in that proxy URL
as empty-username URL userinfo (`http://:<token>@proxy`). The token is not
copied into the launch frame or command arguments. The runner keeps the claim
value in its process environment for the runner protocol and MCP.

The Secret-backed claim migration and the role-based rename of the shared
decision-endpoint credential are deferred in <../sandbox/TODO.md>.

## Proxy authentication and decision flow

The proxy accepts the session token from `Proxy-Authorization` as either:

- `Bearer <token>`; or
- `Basic <base64(:token)>`, with an empty username.

It removes that header before forwarding upstream. For CONNECT, it carries the
parsed token from the outer flow to the inner request. Missing or malformed
proxy credentials receive a local `407` and never reach the upstream.

An authorization denial receives `403` with `X-Haku-Egress-Denied` carrying the
decision reason, `X-Haku-Egress-Help` carrying stable grant-request guidance,
and, when the decision identifies a grantable origin, `X-Haku-Grant-Scope`
carrying its exact `scheme://host:port`. The same guidance is included in the
response body: headers remain visible when a refused HTTPS `CONNECT` hides its
body, while the body gives plain-HTTP and intercepted inner requests a readable
next step.

For every admission, the proxy:

1. resolves the complete bounded address set for the connection target;
2. sends the shared fence credential in the `Authorization` header, plus the
   session token, pinned address, and connection/request metadata in the
   decision body to Console;
3. refuses denied, malformed, unavailable, or otherwise unclassifiable
   decisions;
4. pins the upstream dial to the address validated by that decision; and
5. applies only the returned request-specific substitutions before forwarding.

The request metadata comes from the connection target and decrypted request,
not from a caller-controlled `Host` header. Bodies stay out of the decision
call. A CONNECT is decided by origin first; intercepted inner requests are
decided separately with method and path-plus-query metadata.

## Console decision policy

Console evaluates configuration-file HTTP grants first, then the authenticated
Agent/session's active database HTTP grants after a clean configuration-grant
denial. A decision may return an exact grant scope, expiry, and substitutions.

The policy boundary includes:

- canonical exact HTTP origins, with optional method and full-match
  path-plus-query coverage;
- complete DNS-answer validation and selected-address pinning, including
  rejection of prohibited or mixed public/prohibited answers unless an exact,
  destination-scoped internal-service override applies;
- the proxy pod's Cilium egress ceiling, which Console cannot widen; and
- fail-closed behavior on Console failure, timeout, malformed responses,
  resolution failure, or substitution failure.

Temporary grants and credential-use decisions remain separate authorities even
when one decision call returns both outcomes. Dynamic authorization decisions
are not cached in the proxy.

## Credential substitution

The sandbox presents inert, non-secret placeholders. Console returns a
request-specific substitution only after it has authorized the exact request
for the live Agent/session. The proxy scans only the named headers, including
inside a `Basic` payload when configured, and keeps real credential values out
of logs, metrics, cache, configuration, and persisted records.

An unrecognized or unscanned placeholder occurrence passes through unchanged;
the placeholder is not itself an upstream credential. The proxy never chooses
an Agent or credential based on client IP, network position, request JSON, or
the shared fence credential.

## Non-negotiable invariants

- Every HTTP decision has a live session token or is denied.
- The session token is the same per-Session secret used for MCP and HTTP.
- The shared fence credential authenticates only the shared fence to Console;
  it carries no Agent binding.
- Removing proxy environment variables does not create direct egress; Cilium
  prevents bypass around the proxy.
- The decision endpoint is localhost-only and unreachable from sandboxes.
- Credentials and bearer values never appear in proxy logs, launch frames,
  argv, or persisted session records.
- Any authority, parsing, DNS, pinning, or lifetime failure fails closed.

The current follow-up work is tracked in <TODO.md>. The naming TODO is
intentional: `HAKU_EGRESS_FENCE_CREDENTIAL` and the `fence-credential` Secret
key remain deployed until a role-based replacement is made atomically.
