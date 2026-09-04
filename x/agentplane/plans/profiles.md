# Profiles

Status: **deferred, pending design**. Nothing in the code, the CRDs, or the cluster manifests
implements a profile today, and nothing may add one until the design below exists.

A profile is the preset a sandbox runs under — "public coder", "Haku" — the named bundle of what
an agent in that sandbox is allowed to do. The pull toward it is real: an operator creating a
sandbox wants to pick one thing, not tick a list of policies.

## Why it is deferred rather than built

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

## What leaving looks like

This entry burns down when a design says what a profile is, where it lives, and how every
consumer — egress, approvals, whatever else has arrived by then — reads it. That design graduates
to a doc under `x/agentplane/docs/`, and this file goes.

## Meanwhile

A sandbox's egress is exactly the policies picked for it at creation, as one binding it owns.
Presets are approximated by picking the same policies again; nothing infers a class of sandboxes.
