# Agent HTTP egress control plane — research and options

This document records the research, rejected alternatives, and design history
behind Haku's HTTP egress control plane. It is not the implementation contract.
The current contract lives in <../../../haku/egress/SPEC.md> and should be
updated when the converged system changes.

Tracking issue: [#4670](https://github.com/agentydragon/ducktape/issues/4670).
The authentication split and session-bearer work landed under
[#5139](https://github.com/agentydragon/ducktape/issues/5139).

## Research question

Haku Console needs to control whether an authenticated Agent may initiate an
outbound HTTP(S) connection and whether that Agent may use a Console-managed
credential on the resulting request. The design space considered:

1. deploy-managed standing destination policy;
2. time-boxed, operator-approved destination grants;
3. destination-scoped credential substitution selected by opaque placeholders;
4. request-scoped API capabilities that could replace custom provider backends
   where their semantics can be expressed safely over HTTP.

The current implementation is a shared mitmproxy-based fence colocated with
Console and a single Console↔proxy decision call. The shared fence credential
authenticates the proxy/fence to that endpoint; the live session token supplies
Agent and session identity. See <../../../haku/egress/SPEC.md> for the
security and wire contract.

## Decision record (ruled 2026-08-27)

**mitmproxy is the adapter, embedded as a library.** Its intercepted-HTTP/2
credential substitution was measured, and its h2/gRPC MITM covers the hardest
data-plane requirement: `bbr` → BuildBuddy is gRPC, so a fence that only
tunnels CONNECT cannot serve it. Embedding, rather than running a `mitmdump`
addon script, makes fail-closed behavior a property of our code: stock addon
exceptions fail open and `Flow.kill()` is unreliable mid-stream, whereas
owning the process lets an error anywhere in the decision path terminate the
connection.

**The Console↔proxy API is ours to design.** With one adapter there is no
cross-proxy line-protocol compromise, so one decision call —
`POST /api/internal/http/decide` — carries both the reachability verdict and
request-specific credential-substitution operations. This replaced the earlier
two-facade `/api/internal/http/authorize` plus
`/api/internal/http/credentials/redeem` split.

### Rejected adapters

- **iron-proxy:** no per-request hook mechanism, so it cannot ask Console
  anything. It remains a separate static fence while the colocated proxy is
  introduced.
- **Squid:** lacks h2 MITM, and its response-caching advantage is a v1
  non-goal. Its helper/conformance questions died with the Squid spike.
- **ICAP:** the REQMOD seam was implemented for Squid, but it fell with that
  adapter; the ruled seam is the one internal decision call.
- **Envoy:** no reusable dynamic-certificate machinery for this forward-proxy
  use case; its stream-lifetime controls remain useful reference material.
- **Purpose-built broker:** retained as the fallback if no existing adapter
  could be made conforming; embedded mitmproxy removed the need to build TLS
  interception from scratch.

### Measured mitmproxy behavior

These observations constrain the implementation:

- intercepted HTTP/2 requests support credential substitution;
- streaming remains incremental only when enabled before body access;
- addon exceptions forward by default, and `Flow.kill()` cannot reliably kill
  flows already in transit, requiring a structural deny backstop;
- callbacks may need thread-safe scheduling onto mitmproxy's event loop.

## Open design threads

These are research threads, not alternate descriptions of the current contract.

### Request-scoped API capabilities

Exact-origin grants are the safe first abstraction. A future typed capability
could authorize a reviewed provider operation such as
`gmail.users.messages.get`, with explicit method, path parameters, and query
constraints. It must use upstream-equivalent parsing rather than arbitrary
caller-supplied regular expressions, and must distinguish broad list/get
coverage from exact-object or mutation authority.

Candidate migrations discussed during the spike:

- **Grocy:** conventional REST and API-key presentation make reviewed route
  allowlists a plausible first direct-HTTP surface; writes need body schemas.
- **Gmail:** Discovery operation IDs could define a reviewed read-only subset;
  send, modify, batch, upload, and attachment operations need additional body
  and response modeling.
- **Kubernetes:** direct HTTP may reuse the transport path, but authorization
  must continue through canonical `RequestAttributes`, not generic URL rules.

### Public addresses and DNS pinning

The adapter should authorize the complete bounded DNS answer, reject prohibited
or mixed public/prohibited answers, and dial the selected address without a
second resolution. The exact prohibited classes and any internal-service
override belong to the current contract in `haku/egress/SPEC.md`.

### Cilium ceiling

Console policy cannot widen NetworkPolicy. A temporary grant can select only a
destination admitted by both Console and the proxy pod's Cilium egress policy.
Any widening of the proxy's public-Internet ceiling is a separate GitOps review,
not a grant side effect.

## Related work

- [#3898](https://github.com/agentydragon/ducktape/pull/3898): initial proxy survey
- [#4023](https://github.com/agentydragon/ducktape/pull/4023): Console-authoritative decisions and per-Agent fences
- [#4031](https://github.com/agentydragon/ducktape/pull/4031),
  [#4036](https://github.com/agentydragon/ducktape/pull/4036), and
  [#4037](https://github.com/agentydragon/ducktape/pull/4037): Squid and ICAP experiments
- [#4038](https://github.com/agentydragon/ducktape/pull/4038): Console credential ownership
- [#4046](https://github.com/agentydragon/ducktape/pull/4046) and
  [#4051](https://github.com/agentydragon/ducktape/pull/4051): Squid and mitmproxy comparison
- [#4113](https://github.com/agentydragon/ducktape/pull/4113): temporary Kubernetes grants
- <../../../haku/kube_api_proxy/README.md>
- <../../../docs/personal_agents/credential_proxy.md>

## References

- <https://docs.mitmproxy.org/stable/api/events.html>
- <https://github.com/mitmproxy/mitmproxy/blob/main/mitmproxy/flow.py>
- <https://www.envoyproxy.io/docs/envoy/latest/api-v3/config/core/v3/protocol.proto>
- <https://developers.google.com/workspace/gmail/api/reference/rest>
- <https://kubernetes.io/docs/reference/access-authn-authz/authorization/>
- <https://agentgateway.dev/blog/2026-07-27-credential-injection-ai-agent-egress-cb4a/>
- <https://infisical.com/blog/agent-proxy>
- <https://hermes-agent.nousresearch.com/docs/user-guide/egress/iron-proxy>
