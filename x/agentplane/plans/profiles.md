# Profiles

Status: **deferred after the launch-presets first slice landed**. The scoped implementation is
documented in [`../docs/launch_presets.md`](../docs/launch_presets.md); this file remains as the record of why a broad
capability profile is not the first abstraction.

A broad profile would be the preset a sandbox runs under — "public coder", "Haku" — including
what an agent is allowed to do. The pull toward a launch preset is real: an operator creating a
sandbox wants to pick one thing, not tick a list of policies. The first slice deliberately narrows
that product concept to app-owned SandboxPreset and ThreadPreset defaults; capability policy remains
owned by its existing authorities.

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
removed for this reason, not merely unused: a subject is one named Sandbox.

**Profiles are not to be stored in Kubernetes** (Rai). Where they do live is part of the design
this entry is waiting on.

## What this leaves open

The launch-presets design says where the app-owned defaults live and how the current Sandbox and
Thread consumers use them. A broader capability profile still waits for a design that says how
future consumers — egress, approvals, MCP reachability, and other tool permissions — share one
authority. Do not widen the launch-presets slice to settle that question.

## Meanwhile

A sandbox's egress remains exactly the policies picked for it, at creation or granted afterwards,
one binding it owns per grant. The launch-presets slice may prefill those choices, but normal egress
authorization and enforcement still apply.
