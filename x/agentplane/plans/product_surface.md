# Agentplane product-surface inventory

This document records desired capabilities without assigning priority or implementation sequence. The
[Agentplane task DAG](task_dag.md) decides sequencing only after native-driver evidence exists.

## Product shape

Agentplane should be a standalone, headless orchestrator service. A conversation-style UI or
integration app should consume its API and own the user-facing presentation. The app may initially be
separate and may later be built into or hosted by Haku Console; that hosting choice is deliberately
open and must not become an accidental shared runtime or persistence dependency.

```text
Claude/Codex native adapters
          |
    shared stdio driver protocol
          |
   standalone Agentplane service
      |                 |
 user/control API     live events
      |                 |
 conversation app / integration app
          |
 Kubernetes deployment behind Authentik
```

Agentplane and Haku Console remain separate products. Haku Console integration, if wanted later,
is an explicit client/adapter relationship rather than shared runtime, database, or frontend code.

## Desired capabilities

### Deployment and access

- Run Agentplane as its own Kubernetes deployment/service.
- Provide a stable externally reachable route for the UI/API.
- Put the route behind Authentik for private access.
- Keep runner Pods, PVCs, readiness, and workload lifecycle under the Kubernetes/Agent Sandbox
  boundary.
- Make the service's health, readiness, and provisioning failures visible to its clients.

### User/control API

The service should eventually expose a deliberate API for the app in front of it, including:

- create or launch a conversation/Thread;
- list Threads;
- retrieve a Thread timeline and current status;
- submit Inputs;
- observe live assistant, tool, provisioning, and terminal events;
- interrupt or steer where the provider supports it; and
- expose stable Thread metadata needed by clients.

Archiving should initially mean “remove from the active view while retaining history,” not deletion
or an automatic retention policy.

The app layer may own LLM-generated Thread names, name-generation prompts, name persistence, archive
presentation, and other presentation/product behavior. A name can later be persisted through an
explicit Agentplane metadata API if multiple clients need it, but naming is not a native-driver or
bridge concern.

The API should expose stable Agentplane concepts such as Thread, Input, Turn, Event, status, and
outcome. It should not require clients to understand native Claude or Codex frame shapes.

### Separate UI / integration app

A separate app in front of Agentplane should eventually provide:

- Thread launch and provider selection;
- names and archive controls;
- active/archive views;
- transcript and tool-activity display;
- live progress and terminal outcome;
- honest unavailable/failed/uncertain states; and
- controls for supported interrupt/steering operations.

The first UI may be intentionally small. Its value is proving that the API supports a real user
workflow, not demonstrating a complete dashboard. It is a client of Agentplane, not a second owner of
runner lifecycle or native continuity.

## API transport direction

### Recommended starting point

Use ordinary REST with an OpenAPI contract for resource reads and commands, plus SSE or WebSocket for
live Thread events. This is the lowest-friction choice for a browser-facing app, easy to inspect and
replay, and sufficient for one Agentplane service with runner adapters behind it.

Keep the API implementation independent of Haku Console's existing routes and models. The API can
start in the same repository as Agentplane while remaining a distinct package and deployment.

### gRPC question

gRPC is worth reconsidering if Agentplane later has independently deployed runner workers, needs
strongly typed bidirectional streaming between services, or develops non-browser clients that benefit
from generated stubs. It is not necessary to make the first standalone service runnable and would
add browser gateway, schema, and operational complexity before those requirements exist.

A useful future option is REST/OpenAPI at the user boundary and gRPC internally, but that should be
introduced only after a concrete internal service boundary or streaming limitation appears. Do not
make the native stdio driver protocol resemble gRPC merely for symmetry.

## Layering for identity, policy, and execution

If secure HTTP egress and privileged tool calls are added later, keep the responsibilities separate:

```text
Agent/runner
    -> local fixed-operation proxy
    -> trusted egress gateway
       -> Agentplane identity resolution: Pod -> Sandbox -> Thread -> Agent
       -> Access Controller policy: allow | deny | user approval required
       -> allowlisted upstream operation with real credential at gateway only
```

Agentplane should remain the authority for the live workload-to-Thread mapping. A narrowly scoped
internal endpoint may resolve a verified Pod identity to its current Thread/Agent. Prefer having the
gateway verify the Kubernetes token first and pass a verified Pod identity to Agentplane; do not turn
Agentplane into a general Kubernetes-token introspection oracle unless that is actually required.

An eventual **Access Controller** is a separate, conditional component for decisions such as whether
this Thread may perform a requested operation and whether the operation requires explicit user
approval. It should not own Pod/Sandbox lifecycle or hold upstream credentials. The egress gateway
executes only the controller's authorized decision and remains responsible for narrow destination,
method, path, payload, redirect, private-address, and replay enforcement.

The conversation app—or a later Haku Console surface—should present `user_approval_required` requests
and return Rai's explicit decision without requiring copy/paste through a CLI. Conservative defaults
with a high explicit-approval ceiling are a desired future property, not an Agentplane v0 dependency.

## Deferred Haku Console-adjacent capabilities

Haku Console already contains or may eventually provide capabilities that Agentplane could consume,
but they are deliberately not on the current schedule:

- Agent credential management and egress-time credential substitution;
- user-approved tool calls;
- per-Agent auto-approval policy; and
- explicit integration between the Agentplane API and Haku Console authority.

These should not leak into the first Agentplane service as accidental dependencies. Haku Console may
eventually host the conversation app or provide an integration surface, but it should consume the
Agentplane API rather than merge runtime, route, frontend, or persistence ownership. When any of these
capabilities become important, define an explicit integration contract and ownership boundary first.

The current Ducktape-only agent lane is intentionally broader: internet access plus the scoped GitHub
credential used for `agentydragon-agent` is acceptable for that work. This is a lane-specific
operational boundary, not a reusable permission model. A private Haku agent with access to Rai's
personal context needs a separate Agent Console design track for tool execution, HTTP/API calls,
credential handling, approvals, and policies that remain reliable under pressure. Routine per-action
approval clicks are not the foundation for that design.

## Deferred sandbox-bound request identity

A useful future capability would let a downstream MCP server or Kubernetes service authenticate that
a request came from the specific Agent or Thread running in a specific sandbox, while ensuring the
Agent never receives the real credential.

A possible future flow is:

```text
integration app requests Thread + selected MCP server/credential binding
        |
Agentplane records the binding and starts the Thread's sandbox
        |
sandbox-side runner or Agent Gateway obtains a non-secret, scoped identity proof
        |
proxy/service validates the proof as belonging to this Thread/sandbox
        |
proxy substitutes the real credential only at the egress/service boundary
```

The sandbox egress composition is now accepted in [the ADR](adr_sandbox_proxy_gateway.md), validated
by the [observed identity evidence](../docs/sandbox_egress_identity_evidence.md), and implemented by
the [egress proxy](../egress/README.md): the local proxy holds only an
audience-scoped Pod-bound token, while a trusted external gateway holds real upstream credentials.
The following questions remain before scheduling the production implementation:

- What authority binds Agent, Thread, Sandbox Claim, Pod, and credential without trusting agent-
  supplied claims?
- Is the proof minted by Agentplane, the Sandbox controller, an Agent Gateway, or a separate
  identity service?
- How does the verifier detect replay, copying to another sandbox, Pod replacement, and stale
  Threads?
- Does the proof identify a Thread, an Agent, a runner instance, or a narrower request capability?
- Where is the real credential substituted, and how do we prove it never reaches the Agent,
  environment, native frames, logs, or workspace?
- Can an MCP server validate the proof directly, or must all such traffic pass through a trusted
  gateway?
- What does Agent Sandbox's roadmap or Agent Gateway actually guarantee, versus merely propose?

A token held in a harness-runner process could be part of the solution only if that runner is a
trusted process isolated from the Agent. Process memory alone is not a security boundary when the
Agent can inspect the same container, user, PID/IPC namespace, filesystem, inherited descriptors, or
local sockets. A future design must establish what prevents the Agent from observing or invoking the
credential-bearing process.

This makes the Agent Sandbox team's roadmap and any Agent Gateway design especially relevant
research inputs. We should inspect their actual guarantees before choosing between a trusted runner,
sidecar, gateway, or another proof mechanism; do not assume the roadmap provides a current product
contract.

When this is eventually scheduled, start with the smallest external-gateway implementation and an
end-to-end proof of sandbox-bound authentication and secret exclusion. Agentplane should supply the
authoritative Pod -> Sandbox -> Thread/Agent mapping once it exists; an Access Controller may consume
that identity for allow/deny/escalation decisions without owning workload lifecycle or upstream
credentials. Do not add a generic identity/fencing protocol to the first credentialless Agentplane
service in anticipation of this work.

### Isolation is a separate boundary from harness driving

The harness driver should not be responsible for protecting egress credentials. Its job is to drive
the native Claude/Codex protocol and record provider behavior. Credential confidentiality belongs to
the sandbox/runner/egress composition around that driver.

A local proxy with an unauthenticated transport can be reasonable in a strongly isolated sandbox:
the placement boundary says which sandbox can reach the proxy, while the proxy enforces a narrow,
non-generic operation policy and substitutes the real credential. But “localhost” or an unauthenticated
socket is not protection when Agent-controlled code shares the proxy's process/container/security
domain. A generic forward proxy would also be an exfiltration oracle: the Agent could ask it to send
the credential to an attacker-controlled destination or abuse a signing operation. The proxy must
therefore constrain destinations, operations, and request shapes, and direct egress must be blocked.

The implementation consequence is deliberate separation: capture and adapter features can be
implemented and tested without solving credential isolation; the later isolation proof can wrap the
same runner/driver seam without changing native harness semantics. Warm pools are not a near-term
requirement. If a later threat model demands a harder boundary than ordinary containers and sidecars,
evaluate a Firecracker microVM or comparable VM isolation as a deployment/runtime option; do not
assume Agent Sandbox currently provides that integration, and do not couple the driver protocol to it.

The current Agent Sandbox shape appears able to carry this composition. Its `SandboxTemplate` embeds a
Kubernetes PodSpec, and the repository's current sandbox documentation explicitly describes mirroring
a Secret into the Sandbox namespace and referencing it from the template. A current Codex template
already uses a Secret key reference for an LLM virtual key. A later disposable validation should test
the file-volume form and mount the Secret only into a trusted proxy container, not into the runner
container. The runner can then reach a local proxy without receiving the Secret bytes.

With no warm pools, there is no pre-warmed-Pod adoption or credential-reassignment ambiguity in
this design. A per-Thread Secret still requires a per-Thread provisioning path: a template-level
Secret reference remains shared by every Pod using that template, while a Claim-specific Secret or
PodSpec must be created and bound before the sandbox starts. Secret rotation must also be tested for
both mounted files and proxy reload behavior.

If policy or lifecycle constraints prevent Secret mounting into the proxy container, a fallback is for
a trusted runner to receive a short-lived credential from the control service and hand it to the proxy
through a one-shot private channel, then erase its copy. That still requires the runner to be a real
security boundary from the Agent; otherwise the fallback merely moves the exposure from the proxy to
the runner. The Agent may be able to interrupt or misuse a narrowly constrained proxy, but it must not
be able to turn it into a generic forwarding or signing oracle.

## Decisions deliberately left open

- Authentik forward-auth versus Agentplane's own OIDC session handling;
- REST event polling versus SSE versus WebSocket for the first client;
- whether the API and UI ship in one Agentplane deployment or as separate deployments;
- PostgreSQL schema and migration packaging;
- whether a future internal runner boundary benefits from gRPC;
- which provider controls are exposed in the first UI; and
- the eventual authority and proof format for sandbox-bound request identity.

These are requirements and design questions, not a priority ordering. The immediate implementation
constraint remains: prove the shared protocol and both native adapters before building broad API or
UI machinery.
