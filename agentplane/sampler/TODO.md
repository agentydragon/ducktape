# TODO — sampler

## Compare sampler and notification-service notifications

Consider whether the sampler's Codex-derived in-turn notifications and the Notification Service's
notifications should share a mechanism. Compare their recipients, delivery timing, ordering,
durability, authority, and recovery semantics first; unify them only if those contracts fit.

## Define model continuation compatibility

Decide who owns same-API model transition policy and where to configure it. Establish compatibility
for provider accounts, connections, model families, dialects, and tool encodings, including how to
identify the source contract after its route is retired. Test which providers can replay ordinary
history, encrypted reasoning and opaque compaction items after a switch. Use that evidence to
decide when a switch can proceed and what recovery to offer when a retained context cannot move.

## Evaluate Anthropic Message Batches for agent turns

Investigate whether the central sampler can run a useful, delay-tolerant agent loop through the
[Anthropic Message Batches API](https://platform.claude.com/docs/en/build-with-claude/batch-processing).
A batch can contain [one request](https://platform.claude.com/docs/en/api/http/messages/batches/create)
and receives the 50% batch discount, but results are asynchronous and not streamed. Client-side
tool calls would require executing the tool and submitting a continuation; do not assume the
existing synchronous Messages adapter or Claude Code sessions already support this.

Run a small API-key experiment with single-request batches and real tool continuations. Record
per-step submission, processing and result latency, cache writes and reads, token charges and
end-to-end cost against the ordinary Messages path. Compare 5-minute and 1-hour prompt-cache TTLs:
the cache clock starts when the request using it starts, so processing and queue delays can consume
the reuse window. Include missed cache hits and batch expiry in the comparison.

If the economics and latency are useful, identify how batch IDs, polling, cancellation, durable
turn state and recovery fit the sampler's existing ownership and journal boundaries; preserve
uncertain-effect and no-replay guarantees for client-side tools. Report whether a supported
delay-tolerant Agentplane workflow justifies an implementation, rather than assuming that all
interactive sessions should use batches. This TODO does not authorize a runtime cutover.

## Revisit conversation retention across the central sampler and History

The central sampler retains conversation items and exports journal entries covering the same
conversation to History. How much of that overlap do we need? If the sampler only needs the
current context window to continue a conversation, could it discard superseded items after
successful compaction? If the sampler keeps the full conversation, could History read older
items from it on demand, or could the two services share one retained copy?
