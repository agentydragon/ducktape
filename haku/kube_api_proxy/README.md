# Haku Kubernetes API proxy

This is the first, deliberately narrow implementation of the inline authorization
proxy tracked by [#4428](https://github.com/agentydragon/ducktape/issues/4428).
It is a separate Go binary: Haku Console remains the Kubernetes authorization
authority, while the proxy is the only Kubernetes request path available to an Agent.

```text
Agent --Haku bearer--> kube API proxy
                       |  classify Kubernetes API path and verb
                       |  POST bearer + attributes + minimal PolicyRule
                       v
                    Haku Console
                       |  allowed + SAR/grant source and decision
                       v
                    kube-apiserver <-- proxy-held in-cluster credential
```

The structural property is fail-closed authority. The proxy asks Console before
every request and bounds the forwarded request context by its configured request
timeout. If Console or the Kubernetes authorization API is unavailable, no
request is sent to Kubernetes. The Console asks Kubernetes for a
SubjectAccessReview using a fixed, deploy-configured subject; the Agent cannot
choose the Kubernetes username or groups.

## Implemented

- Kubernetes apiserver's `RequestInfoFactory` maps method/path/query to API
  group, resource, subresource, namespace, object name and verb. Depending on
  the real implementation avoids maintaining a security-sensitive parallel
  parser and keeps selector behavior aligned with kube-apiserver.
- The proxy derives the minimal equivalent resource or non-resource `PolicyRule`
  and sends it to Console with the original bearer in the `Authorization`
  header. The credential is never copied into JSON or logs.
- It requires HTTPS for the Console authorization hop unless an explicit
  development-only override is set, and never follows redirects with the bearer.
- If allowed, only ordinary representation/cache headers are forwarded. Caller
  authorization, cookies, API keys, proxy metadata and Kubernetes
  identity/impersonation headers are removed; the in-cluster transport supplies
  the upstream credential.
- Authority failures, malformed decisions and denials all fail closed.
- Request bodies, authorization calls and ordinary Kubernetes requests are bounded.
- Pod `exec` and `portforward` upgrade requests are forwarded transparently.
  The initial authorization expiry is a hard stream deadline, and Console is
  rechecked every five seconds so release, revocation or authority failure
  closes the stream within the revalidation interval plus the authorization
  timeout—eight seconds with the deployed defaults. Port-forward therefore
  does not outlive its grant: after a grant is withdrawn, its connections close
  within that bound.
- Following a pod log streams the chunked response through without buffering it,
  under the same stream lifetime enforcement as exec and port-forward.
  Kubernetes RBAC does not distinguish following a log from reading one — both
  are `get` on `pods/log` — so an Haku grant cannot either. Whether a request
  follows is decided by apimachinery's own boolean parameter conversion, whose
  semantics are not `strconv.ParseBool`: only an absent value, `0` or a
  case-insensitive `false` is false, every other value is true, whitespace is
  significant, and a repeated parameter is decided by its first value alone.
- A `watch` streams through under the same lifetime enforcement, authorized as
  the `watch` verb on the watched resource. Which requests are watches comes
  from `RequestInfoFactory`, which decodes `ListOptions` with the parameter
  codec kube-apiserver itself uses and resolves the deprecated
  `/api/{version}/watch/` path prefix to the same verb — so a request the proxy
  authorizes as a watch is exactly one kube-apiserver will stream. A `watch`
  query parameter on a named object is not a watch, because kube-apiserver
  serves named objects through its bounded `get` handler.
- Resource proxying, `attach` and other upgrades return `501` before
  authorization or forwarding.
- Console exposes the typed endpoint contract at
  `POST /api/internal/kubernetes/authorize`. It remains unavailable (and thus
  fail-closed) until standing SAR subjects are explicitly mapped to Agent access
  profiles.

## Stream lifetime and limits

A long-lived request — exec, port-forward and a followed pod log — is bounded by
its authorization decision rather than by `HAKU_KUBE_REQUEST_TIMEOUT`, which
applies only to ordinary bounded requests. A decision carrying `valid_until`
becomes a hard deadline; every decision is rechecked each
`HAKU_KUBE_STREAM_REVALIDATION_INTERVAL`, so release, revocation or authority
failure ends the stream within that interval plus the authorization timeout.

Two limits are deliberately absent, for different reasons.

A stream authorized by standing SAR policy carries no `valid_until` and therefore
has no absolute lifetime. Its authority is still bounded, because revalidation ends
it once the standing decision stops allowing it. Whether to bound its duration too
is open, and belongs with the operational hardening in
[#4562](https://github.com/agentydragon/ducktape/issues/4562).

There is no cap on concurrent streams per Agent, and that one is settled rather than
pending: leaving watches open is worth more on this cluster than the ceiling a cap
would add. Add one only if real resource exhaustion argues for it.

Ending a stream cancels the upstream request after the response headers have been
sent, so the caller observes a truncated response rather than a status code:
`ErrorHandler` cannot run once a body has begun. This is how kube-apiserver
itself ends a watch, and an ordinary Kubernetes client re-lists and receives the
new denial.

## Console authorization contract

Example request:

```json
{
  "attributes": {
    "resource_request": true,
    "verb": "get",
    "api_version": "v1",
    "namespace": "demo",
    "resource": "pods",
    "subresource": "log",
    "name": "web",
    "path": "/api/v1/namespaces/demo/pods/web/log"
  },
  "required_scope": {
    "kind": "namespaces",
    "namespaces": ["demo"]
  },
  "required_rules": [
    {
      "api_groups": [""],
      "resources": ["pods/log"],
      "verbs": ["get"],
      "resource_names": ["web"]
    }
  ]
}
```

The proxy forwards the Agent's original `Authorization: Bearer ...` header over
HTTPS. A successful response is:

```json
{
  "allowed": true,
  "source": "sar",
  "decision_id": "sar:4f5c..."
}
```

Console should return `allowed: false` for a valid identity denied by the
standing policy, `401` for an invalid Haku identity, and a non-2xx response
when the Kubernetes authorization API cannot be read. The proxy fails closed
on every non-2xx or malformed response. Allowed decisions require a non-empty
`decision_id`. Standing SAR decisions omit `valid_until`; a later temporary-grant
decision may include it, in which case the proxy terminates the upstream request
at that instant.

Port-forward authorization deliberately follows Kubernetes RBAC semantics: the
decision covers `create` on `pods/portforward` for one named pod. The requested
`ports` query parameter is forwarded unchanged, but it is not a Kubernetes RBAC
attribute, so an Haku grant cannot constrain individual remote ports.

Current `kubectl` first attempts a WebSocket `GET` port-forward handshake,
whereas legacy clients use an upgraded `POST`/SPDY request. Kubernetes
authorizes both forms as `create pods/portforward`; the proxy therefore maps
only a WebSocket handshake with a `Sec-WebSocket-Protocol` value to that
canonical authorization verb. An ordinary `GET` remains a `get` request and is
rejected before forwarding.

## Configuration

| Environment                              |            Default | Meaning                                             |
| ---------------------------------------- | -----------------: | --------------------------------------------------- |
| `HAKU_KUBE_AUTHORIZATION_URL`            |           required | Absolute HTTPS Console authorization URL            |
| `HAKU_KUBE_ALLOW_INSECURE_AUTHORITY`     |            `false` | Development/test-only plain-HTTP opt-in             |
| `HAKU_KUBE_LISTEN_ADDRESS`               |            `:8080` | Proxy listen address                                |
| `HAKU_KUBE_AUTHORIZATION_TIMEOUT`        |               `3s` | Maximum Console decision latency                    |
| `HAKU_KUBE_REQUEST_TIMEOUT`              |              `30s` | Maximum ordinary Kubernetes request lifetime        |
| `HAKU_KUBE_STREAM_REVALIDATION_INTERVAL` |               `5s` | Interval between active-stream authorization checks |
| `HAKU_KUBE_MAX_REQUEST_BYTES`            |         `10485760` | Maximum request body size                           |
| `HAKU_KUBE_SERVICEACCOUNT_DIRECTORY`     | Kubernetes default | Projected CA and token directory                    |
| `KUBERNETES_SERVICE_HOST`                |           required | In-cluster Kubernetes API host                      |
| `KUBERNETES_SERVICE_PORT_HTTPS`          |           required | In-cluster API port (`..._PORT` fallback)           |

The Kubernetes API address, CA and rotating projected bearer are loaded from
Kubernetes' standard in-cluster environment and ServiceAccount files. This is
the narrow in-cluster information needed by the proxy. Environment variables
are decoded and validated together with `github.com/caarlos0/env/v11`; malformed
configured durations, booleans and sizes stop startup rather than silently
falling back to defaults.

Console-side SAR authorization is separately opt-in. Its production-safe
default is unset, which leaves the internal endpoint unavailable and the proxy
denied. Deployment configuration maps `subjects_by_access_profile` entries to
fixed Kubernetes usernames/groups and may set a bounded `timeout_seconds`.
Console validates the Haku bearer through the same MCP bearer authority:
configured static-Agent credentials, active Console-minted session bridge
bearers, and verified MCP OAuth access tokens all resolve to a canonical Agent.
The proxy never accepts the Console's browser-session principal. Console uses
the resolved Agent's deploy-managed access profile to select a subject, and
fails closed when that profile has no configured subject or the bearer cannot
be revalidated.

## Deployment status and TODOs

The capability-neutral production cutover deploys this proxy at
`haku-kubeapi.allegedly.works`. Public Coder reaches it only through its mandatory
iron-proxy, which substitutes the Agent's Haku bearer for that exact hostname.
The Agent has no Kubernetes credential and its NetworkPolicy has no direct
kube-apiserver path. The proxy uses a rotating projected ServiceAccount token with a deliberately
reviewed `cluster-admin` static ceiling. That ceiling creates no standing Agent authority: Console
still authorizes every request through the fixed SAR subject or an active exact Agent-owned grant.
For this personal cluster, the inline Haku decision is intentionally the effective temporary-access
policy boundary so useful grants do not each require another GitOps RBAC expansion.

Flux orders the cutover so the fixed SAR subject, Console configuration, proxy
Deployment/route, and cross-namespace execution bindings exist before the Agent
proxy changes credential substitution. A partial rollout therefore leaves the
old path intact or fails the new path closed; it never exposes the proxy's
upstream credential to the Agent.

The proxy emits each allow/deny decision to stdout with its decision ID and canonical Kubernetes
request attributes. Console logs the same decision ID with the Agent and grant source. Promtail
ships both pods' stdout/stderr to Loki, whose current configuration has no finite age-based
`retention_period`; the durable grant row completes the grant-to-source-ToolCall link.

Exec and port-forward intentionally use Go's standard reverse-proxy upgrade
path rather than implementing Kubernetes remote-command or port-forward channel
framing. The in-cluster upstream transport stays on HTTP/1.1 because Kubernetes
streaming subresources use protocol upgrades that an HTTP/2 transport rejects.
The first exec support and acceptance contract is noninteractive
(`stdin=false`, `tty=false`); interactive behavior is not yet promised even
where transparent forwarding happens to work.

A client that watches a collection issues a `list` and then a `watch`, so a
grant covering `kubectl get pods --watch` needs both verbs.

Remaining work:

- TODO(#4562): revisit deeper metrics, alerts and failure hardening if operational experience
  justifies them.
- TODO(#4428): consider discovery-response caching only if it preserves the
  fail-closed authority model and never bypasses a SAR decision.
