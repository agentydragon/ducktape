# LLM-Mediated Time Lockout

## Raw Idea

Something like Timekpr, but when the user is locked out they can still talk to
an LLM, such as Claude.ai. The LLM acts as an exoself / superego guardrail:
supportive, memory-aware, and allowed to decide whether the user gets temporary
computer access again.

## Problem

Current Timekpr-style lockouts add unskippable friction. In practice, that makes
them brittle: if the lockout blocks real work or feels too dumb, it becomes
easier to permanently disable the service after boot than to keep living with
it.

The actual need is softer:

- interrupt dopamine spirals
- help the user remember why the guardrail exists
- distinguish real need from reflexive avoidance
- keep some escape path that does not collapse into permanent disabling

## Sketch

When the time limit triggers, the machine enters a restricted mode. Instead of a
hard lockout, the user gets a kiosk browser pointed at an LLM chat interface.

Cheap MVP:

- show only a kiosk browser, maybe on `claude.ai`
- seed the chat with durable instructions describing the guardrail role
- keep long-term context in the LLM provider's own memory if available
- give the LLM an explicit tool that can grant a temporary unlock or extension
  after a conversation

The LLM should not be a punitive gate. It should be closer to a good mini
intervention: listen, reflect the stated goals, ask whether this is a real need,
and help choose the next bounded action.

## Plumbing Assumption

The unlock path does not need to be solved with browser hacks. The LLM can have a
real tool, and the tool can talk to local infrastructure that ultimately controls
the lockout state.

The machines are already part of the broader infrastructure: they are Kubernetes
nodes and are on Nebula. That should make it possible to expose a small,
authenticated control surface somewhere reasonable:

- local agent on the workstation reachable through Nebula
- cluster service that relays tool calls to the relevant machine
- short-lived signed unlock tokens consumed by a local daemon
- MCP-style tool endpoint that wraps one of the above

The exact plumbing can be figured out later; the important bit is that the LLM
gets an intentional capability, not a pretend one.

## Open Questions

- What is the smallest useful unlock primitive: 5 minutes, app-specific access,
  URL allowlist, full temporary unlock?
- How much local state should exist versus relying on Claude.ai memory?
- What prevents the user from stopping the service with `sudo systemctl stop`?
- Should the system log unlock reasons for later review?
- What is the right failure mode if the LLM provider is down or network is
  unavailable?

## Possible Next Step

Prototype the non-security-critical flow first: a local daemon that can enter a
restricted desktop mode, open a kiosk browser, and accept an explicit local
unlock command. The LLM integration can be added after the UX loop feels useful.
