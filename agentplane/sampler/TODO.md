# TODO — sampler

## Evaluate sampler-managed compaction for Messages and Responses

Consider offering the Chat Completions approach as a per-route alternative to provider-native
compaction for Anthropic Messages and OpenAI Responses. Append a configurable summary prompt to
the cached prefix, request text, and replace the compacted prefix with a configurable summary
message. Preserve complete retained turns and their tool exchanges. Compare cache reuse, latency,
and summary quality against the existing provider-native modes.

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
