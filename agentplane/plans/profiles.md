# Profiles

Status: **deferred**. The scoped launch-presets design is documented in
[`../docs/launch_presets.md`](../docs/launch_presets.md); this file records why a broad
capability profile is not the first abstraction.

A broad profile would be the preset a sandbox runs under — "public coder", "Haku" — including
what an agent is allowed to do. The pull toward a launch preset is real: an operator creating a
sandbox wants to pick one thing, not tick a list of policies. The first slice deliberately narrows
that product concept to app-owned SandboxPreset and ThreadPreset defaults; capability policy remains
owned by its existing authorities.

**Modularity boundary:** SandboxPreset remains exclusively integration-app-owned. The app resolves
preset defaults and per-Sandbox additions into concrete egress and Actions bindings; neither
enforcement service knows preset names or depends on the other. Similar composition/scope conventions
do not require a shared runtime profile or policy engine. Policy representation can follow the first
human-approved external OAuth/MCP slice.

## Why the broad profile remains deferred

**A profile is expected to span more than egress.** Per-tool-call approvals and other capability
grants are expected to key off the same notion: whether a tool call needs Rai's decision, which
MCP servers are reachable, what a sandbox may do beyond outbound HTTP. Egress is one consumer of
a profile, and the first, but a design settled from egress alone would be settled by its narrowest
consumer.

**So it must not be an `EgressBinding` subject.** The first implementation made a profile the
value of the label `agentplane.allegedly.works/profile`, stamped on a Sandbox at creation, that a
Flux-managed binding's `sandboxSelector` matched. That gave the concept no owning object — a
profile existed only as a string two unrelated places agreed on — while binding it into the egress
CRD, which pre-commits the cross-cutting design to one consumer and makes the second consumer
either extend the egress resource or invent a parallel notion. The selector subject form is
removed for this reason, not merely unused: a subject is one named object, never a selector.

Storage remains an open design choice, including Kubernetes resources, app configuration, and
PostgreSQL. The [action policies design](../docs/action_policies.md) records why that slice chose
Kubernetes objects with per-object ownership; no blanket Kubernetes exclusion applies.

## What this leaves open

The launch-presets design says where the app-owned defaults live and how the current Sandbox and
Thread consumers use them. A broader capability profile still waits for a design that says how one
selection fills in egress, approvals, MCP reachability, and other tool permissions. Do not widen the
launch-presets slice to settle that question.

A broad profile is an application on top of the separate parts, not a new authority they share
([principles](../docs/principles.md#separate-concepts-mixed-and-matched-no-opaque-bundles)). It
pre-selects existing egress, Action-policy and other grants without duplicating their rules or
taking ownership away from their enforcement services, and a session configured part by part,
with no profile at all, stays fully supported.

## Meanwhile

A sandbox's egress remains exactly the policies picked for it, at creation or granted afterwards,
one binding it owns per grant. The launch-presets slice may prefill those choices, but normal egress
authorization and enforcement still apply.
