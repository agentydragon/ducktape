# Agent operations, approvals, and cross-agent data access

Status: **design discussion; no implementation contract yet**. This note prevents the next
approval feature from turning Agentplane into a monolith before the product nouns and authority
boundaries are settled.

## Why the current “ask” name is provisional

Haku Console's `tool_call` is a useful precedent but is too narrow as the Agentplane product noun:
an operation may invoke an MCP server, use HTTP egress, request Kubernetes authority, read a
trajectory, or ask another service to do work. Conversely, a human approval is only one possible
outcome of an operation and is not the operation itself.

Working vocabulary for discussion:

- **Operation**: an agent-originated intent to invoke a named capability with structured input.
- **Invocation**: one attempt by an adapter to execute an Operation.
- **Decision**: policy or human outcome over whether/how the Operation may proceed.
- **Delivery**: the machine-readable result, denial, pending state, or other event sent back to the
  originating Thread.
- **ApprovalRequest** (or **AccessRequest**): the subset of Operations that need a human decision.

Recommendation: use `Operation` for the durable general object and reserve `ApprovalRequest` for
its gated subset. Do not use “ask” in a wire contract; retain it as UI copy until Rai chooses the
product noun.

The choice Rai needs to make is whether a single durable object covers the whole lifecycle, or
whether authorization is a separate object linked to an invocation. The latter is more explicit
for retries and standing grants; the former is smaller for the first implementation.

## Semantics to settle before implementation

### What an Operation means

Decide whether it is:

- one logical agent intent, with retries and execution attempts beneath it; or
- one concrete execution attempt, where a retry creates another record.

Recommendation: one logical intent with an attempt/result history. This avoids treating a transient
transport retry as a new human approval while keeping each execution auditable.

Every Operation should preserve the upstream adapter payload as the evidence-bearing body. A
normalized mirror of every MCP argument or provider-specific field is not justified yet.

### Lifecycle

A candidate lifecycle is:

```text
submitted
  -> allowed | denied | approval_required
approval_required
  -> approved | denied | withdrawn
approved
  -> running
running
  -> completed | failed | cancelled
```

Potential `expired` state is deliberately undecided. Haku's current approval machinery has no
expiry, while credentials and execution attempts may still have independent time limits. Do not add
an expiry state merely because it is conventional.

Questions for Rai:

- Can the originating agent continue while an Operation is `approval_required`?
- Is the decision delivered as a later Thread input only, or may a blocked native turn be resumed?
- Is withdrawal agent-controlled, operator-controlled, or both?
- Does approval authorize exactly one execution, or may the adapter retry under the same decision?
- Is a standing grant a different object, or a Decision that creates a reusable grant?

Initial recommendation: submission never blocks the agent; approval is an explicit later Decision;
withdrawal is available to the originating agent; one approval covers one logical operation and its
bounded retries; standing authority is a separate grant owned by the existing grants/access
machinery.

### Agent-facing UX

The agent-facing result should be a stable machine envelope independent of whether the adapter is
HTTP, Kubernetes, host execution, or MCP. It should distinguish at least:

```json
{
  "kind": "operation",
  "operation_id": "op_...",
  "status": "approval_required",
  "capability": "mcp:github.search_repositories",
  "input": { "...": "..." },
  "message": "The operator has been notified; continue other work.",
  "decision": null
}
```

The final shape is open. In particular, decide whether the agent receives the full input again,
which could contain sensitive data, or only a redacted/reference form. The human UI needs the full
reviewable operation; the agent may need less.

The operation should be usable for an MCP server without making MCP the core abstraction:

```text
Agent Thread -> Operation adapter -> MCP server/tool
                         -> Decision / result
```

An MCP adapter owns server discovery, tool schema, transport, and MCP-specific errors. The policy
authority decides access and approval; Agentplane does not become an MCP registry or a universal
protocol translator.

## Authority boundary

Keep these authorities separate:

- **Agentplane runtime**: Sandbox and Thread lifecycle, native runner protocol, and delivery to a
  Thread when a product layer asks it to deliver an event.
- **Integration/conversation app**: current operator-facing workflow and presentation, if it owns
  the first Operation UX.
- **Access Controller / grants machinery**: allow, deny, approval-required, standing grants, and
  revocation. It may be a separate service or an existing Haku Console authority; it should not be
  hidden inside Sandbox lifecycle code.
- **Execution adapter**: MCP/HTTP/Kubernetes/host-specific invocation and result translation.
- **Trajectory store**: durable event evidence and operation references, without becoming the policy
  engine.

The first vertical slice may compose these in one deployment for convenience, but it must retain
interfaces that keep the authorities replaceable. Do not add a generic controller, policy DSL, or
MCP gateway until one real consumer requires it.

## Data access and cross-agent reads

Reading a Thread or trajectory is not the same permission as invoking an external tool. A future
cross-agent read needs a policy over at least:

```text
requesting principal
  -> target principal / Thread / data scope
  -> operation (list metadata, search text, read events, read raw frames)
```

Raw frames and search results can carry private data, so “can read the Thread record” is not enough
as a future policy statement. The system may need separate scopes for metadata, derived summaries,
full events, raw native frames, and content-bearing payloads. Do not implement those scopes before a
real cross-agent read consumer establishes the minimum useful boundary.

Agentplane currently has no durable **Agent** entity. A Sandbox is runtime infrastructure and a
Thread is interaction context; neither is a sufficient long-lived identity for statements such as
“Haku may read Public Coder's past conversations.” Before cross-agent reads, decide whether an Agent
is:

- an integration-app-owned identity that may own multiple Sandboxes and Threads;
- an authorization principal supplied by an external access controller; or
- both, with a stable product identity linked to an opaque authorization principal.

Recommendation: keep Agent identity separate from authorization principal, as with the existing
runtime vocabulary. Let the integration app or Agent Console own the mapping from product Agent to
opaque access principal; let the access authority decide read permissions. Agentplane should expose
stable Thread/Sandbox references and enforce a decision at its read boundary, not invent a global
identity and permission registry.

Cross-agent reads are therefore a later design package with two prerequisites:

1. a stable Agent identity and ownership model; and
2. a data-scope/read-policy contract owned outside Agentplane runtime.

Until then, T3 search is scoped to the caller's own authorized trajectory surface, and no API should
pretend that a Sandbox name is an Agent identity.

## MCP and access gating questions

Before wrapping MCP servers, decide:

- whether the MCP server is trusted infrastructure or runs inside the Sandbox;
- whether policy gates server reachability, individual tool calls, or both;
- whether tool schemas and arguments are visible to the operator and to the agent;
- whether an approval authorizes one tool call, a server/tool pair, or a parameterized grant;
- how server-originated content is labeled and protected from prompt injection;
- whether MCP results are copied into the Thread, the trajectory, or both;
- how retries and duplicate tool calls are correlated.

Recommendation: begin with a trusted adapter outside the Sandbox, individual structured tool
invocations, explicit capability names, and one-operation decisions. Let the MCP server remain the
execution authority for its own semantics; let the access authority gate the adapter before it
calls the server.

## Implementation gates

Do not implement the first approval feature until Rai chooses or accepts:

- the product noun (`Operation`, `Invocation`, `AccessRequest`, or another term);
- whether the durable unit is logical intent or execution attempt;
- pending-turn behavior and later-input delivery;
- single-operation versus standing-grant semantics;
- the minimum machine envelope and sensitive-field handling; and
- whether the first MCP adapter belongs in the integration app or an external access/adapter layer.

After those choices, the first implementation should be one end-to-end adapter and one acceptance
scenario, not a universal access framework.
