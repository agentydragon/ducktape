# Agentplane workload authentication and credential propagation

Status: **accepted and implemented foundation.** This records the boundary landed by PRs
[#5685](https://github.com/agentydragon/ducktape/pull/5685),
[#5696](https://github.com/agentydragon/ducktape/pull/5696),
[#5698](https://github.com/agentydragon/ducktape/pull/5698), and
[#5700](https://github.com/agentydragon/ducktape/pull/5700). It is evidence, not an open prerequisite
for the Action Service.

## Implemented path

A managed Sandbox reaches trusted first-party services without exposing a real workload bearer to
the harness or runner:

```text
harness / runner
  Authorization: Bearer agentplane-credential-agentplane-workload
  (inert placeholder)
        |
        v
Pod-local egress sidecar
  Proxy-Authorization: Bearer <sidecar-only Pod-bound token>
        |
        v
central egress proxy
  validates TokenReview + source Pod/live Sandbox
  EgressPolicy selects exact host/method/path and credentialRef
  authenticatedWorkloadToken substitutes the already-authenticated bearer
        |
        +-----------------------------+
        v                             v
authenticated LLM ingress       standalone Action Service
shared SandboxPrincipal         shared SandboxPrincipal
server-held LiteLLM key         service-owned authorization/lifecycle
```

PR #5685 extended `EgressCredential.spec.source` with generic
`authenticatedWorkloadToken`. The proxy retains the bearer only after successful sidecar-hop
authentication and exposes it to the existing exact placeholder/target substitution path. It never
blindly copies a raw `Proxy-Authorization` header, unconditionally appends `Authorization`, mints a
new token, or introduces an LLM/Action service branch. Missing, stale, mismatched, or unbound context
fails closed. Existing `secretRef` behavior remains unchanged.

PR #5696 added the shared destination-side `SandboxPrincipalAuthenticator` and
`SandboxPrincipalResolver`. They require one well-formed Bearer, TokenReview for the configured
audience, an allowed ServiceAccount subject, one Pod name/UID claim pair, the same live Pod UID, and
one controller Sandbox owner. The immutable principal contains namespace, ServiceAccount, Pod, and
Sandbox identity only. It contains no Thread, Agent, operator role, permissions, token, or caller
body/header identity.

The compatibility audience remains `agentplane-egress`. A future coordinated rename to
`agentplane-workload` does not change the contract and is not required for the landed P0 behavior.

## Landed consumers

### Authenticated LLM ingress

PR #5698 added an independently deployable ingress in front of LiteLLM. Staging runner traffic uses
only the workload placeholder. The ingress resolves `SandboxPrincipal`, strips caller credentials and
identity-like metadata headers, forwards provider-native bodies/status/errors/SSE without protocol
translation, and authenticates to LiteLLM with one server-held virtual key. Verified Sandbox
metadata is stamped through LiteLLM's documented metadata header. See
[`../llm_ingress/README.md`](../llm_ingress/README.md).

### Standalone Action Service

PR #5700 uses the same destination-side principal for workload submission and caller-own reads.
Ownership is derived from namespace plus live Sandbox UID, so two Pods sharing a ServiceAccount are
still different callers. The integration app/BFF operator surface uses separate authentication. See
[`../action_service/README.md`](../action_service/README.md).

## Observed acceptance evidence

The landed tests prove:

- two Pod-bound workload tokens substitute per request rather than becoming one static value;
- same-ServiceAccount Pods resolve to distinct live Sandboxes;
- harnesses see only the inert placeholder;
- malformed, wrong-audience, stale/deleted/replaced, and unowned workload identities fail before a
  destination/backend call;
- direct placeholder bypass and forged identity headers/body fields do not establish ownership;
- exact target/host/method/path selection remains the substitution authority;
- provider-native LLM streaming and error bodies pass through unchanged;
- Action Service caller-own/operator-all boundaries use the shared principal; and
- workload and destination credentials are absent from principals, projections, errors, and test
  logs.

The PR descriptions contain the exact remote test/build invocations and BuildBuddy evidence links.

## Deferred

- per-destination audiences if recipient isolation becomes a requirement;
- Kubernetes API access under a distinct projected audience;
- authoritative Thread/Agent attribution after live bindings exist;
- ordinary-service `SubjectAccessReview` integration;
- external-agent authentication, standing grants, cross-agent permissions, credential broker, or
  broad identity/capability framework; and
- shared FastAPI/auth construction deduplication, which may proceed independently without changing
  this boundary.
