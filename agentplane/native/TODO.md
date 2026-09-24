# TODO — Agentplane native harness drivers

Claude Code protocol claims that disagree, or that no test settles, recorded when
`haku/cli_protocol/` was folded into these docs and deleted. `haku/cli_protocol/…` citations are
to that tree at `a9c368d72f`. Either side may be wrong: agentplane's tests may simply never
exercise what the older claim needs. Agentplane pins 2.1.252. Questions black-box probing cannot
settle can go to the operator's separate white-box Claude CLI reverse-engineering project
(`agentydragon/gaffer-private`).

## Scripted compaction test

[Compaction on the wire](../docs/claude_runtime_contracts.md#compaction-on-the-wire-measured-21233-re-verify-on-21252)
was measured live on 2.1.233 only. Add `harness_tests/claude/test_compaction.py` against the pinned
binary, asserting what the deleted capture test held against a recording:

- nothing is retracted: frames before `compact_boundary` include `assistant` and `result`, and no
  inbound `uuid` repeats;
- `compact_metadata.trigger == "auto"`, `pre_tokens > post_tokens`,
  `cumulative_dropped_tokens == pre_tokens - post_tokens`, no `message` on the boundary, and no
  `preserved_segment`;
- the next frame is a `user` frame with `isSynthetic: true` and one `text` block containing
  "ran out of context", text the client never sent;
- `logical_parent_uuid` names no frame in the stream;
- exactly one `PreCompact` `hook_callback`, before the boundary, with `trigger: "auto"`; the only
  `SessionStart` callbacks carry `source: "compact"`;
- every inbound control request was answered;
- `autocompact_state.threshold` predicts the trip point and `get_context_usage.autoCompactThreshold`
  does not, under `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`.

The live run forced compaction with `--autocompact 100000` and a driver-hosted filler tool growing
the context. **Unverified**: whether a scripted endpoint can force it — the trip is the CLI's own
token accounting (inflated `usage` in scripted responses may or may not count), and the
summarisation request must then be recognised and answered by the script.

## Does `--include-hook-events` emit frames for `initialize` hooks?

- Frames appear: `docs/hooks.md:36-37`, `native/claude/scenarios.py:162-163` (the `hooks` launch
  "has the harness report each firing as `hook_started`/`hook_response`"), and
  `runner/SPEC.md:210-211` ("Hook events need `--include-hook-events`"); all 2.1.252.
- None appear: `docs/hooks.md:45-47` (live capture, 2.1.252) and
  `haku/cli_protocol/protocol.md:267-271` (2.1.233, the compaction run passed the flag), for hooks
  registered at `initialize`.

Experiment: in `harness_tests/claude/test_hooks.py`, which already launches with the flag, assert
presence or absence of `system` `hook_*` frames; a settings-file command hook as positive control
would show whether the flag only covers hooks the CLI runs itself.

## What turns `command_lifecycle` on?

- `--replay-user-messages`: `native/claude/wire.py:95`, `native/claude/scenarios.py:159-160`,
  `native/docs/protocol_roster.md:216-218` (2.1.252). Every agentplane test that asserts lifecycle
  frames also sets `replay_user_messages=True`.
- The prompt `uuid` alone: `haku/cli_protocol/protocol.md:184-187` (2.1.220), whose steering probe
  launched without `--replay-user-messages` and still got `queued`/`started`/`completed`.

Experiment: a scripted test sending uuid-stamped prompts without `replay_user_messages`, with a
uuid-less prompt as negative control.

## `UserFrame` does not know the synthetic compaction summary

`native/claude/wire.py:234` describes `UserFrame` as "the replay echo of an input, and tool
results" (2.1.252). On 2.1.233 compaction also emits a non-replay text `user` frame marked
`isSynthetic: true` (`haku/cli_protocol/protocol.md:383-388`), a field `UserFrame` does not model.
By reading, `runner/claude.py` `_on_user` derives nothing from a text-only non-replay frame, so the
summary would reach only the `Native` log. Settle with the scripted compaction test above.

## Notification ack id for driver-hosted MCP

- `docs/driver_tools.md:38-41` and `native/claude/mcp.py:21-23` (2.1.252, marked confirmed): a
  notification needs an `mcp_response` with `"id": 0`; a bare `success` leaves the server `failed`.
- `haku/cli_protocol/protocol.md:300-301` (2.1.220): "an empty JSON-RPC result is the convention".
  Its probes actually sent `{"jsonrpc": "2.0", "id": null, "result": {}}` (echoing the absent id),
  and the tool reached the model (capture, 2.1.233).

Agentplane's claim is newer. The open part is whether `id: null` still works on 2.1.252 or `0` is
required. Experiment: parametrize the ack id (`0`, `null`, omitted) in
`harness_tests/claude/test_driver_tools.py` and assert the server connects and the tool is called.

## Stale "not capture-pinned" statements

`docs/claude_input_queue.md:171` lists `interrupt` with and without `cancel_queued` as still to
capture, and `docs/harness_protocols.md:114-115` says nothing on that page is capture-pinned; but
`harness_tests/claude/test_active_turn.py:101` pins `cancel_queued: true` (per-input `cancelled`)
and `:150` pins a plain interrupt's `still_queued` receipt (2.1.252). `native/docs/protocol_roster.md:41`
also still says "`cancel_queued=False` only". Experiment: none beyond a green run of those tests;
reconcile the docs.

## How long does an unanswered control request wait?

- `haku/cli_protocol/protocol.md:91-94` (2.1.220): an unanswered `can_use_tool` produced no
  `result` within 60 s, read as "waited on indefinitely".
- `docs/hooks.md:31-33` (2.1.252, static reading): `hook_callback` budget is 600 s (30 s for
  `UserPromptSubmit`), and a timed-out `PreToolUse` skips the tool.
- `docs/claude_runtime_contracts.md:218-219`: unanswered human requests stay parked across shutdown.

A 60 s observation cannot tell "indefinite" from any budget above 60 s, and `can_use_tool` is not a
hook. Experiment: a scripted test leaving one `can_use_tool` and one `PreToolUse` `hook_callback`
unanswered, recording time to `result` or timeout notice with a ceiling above 600 s.

## "Capability negotiation" section title

`docs/claude_input_queue.md:104` titles the section "Capability negotiation"; `:106-107` and
`haku/cli_protocol/protocol.md:155-157` (2.1.220) say the CLI only advertises capabilities on
`system/init` and `initialize` has no client-capability field. The section's table (`:109-113`,
from `sdk.d.ts`, version unstated) lists `queued_notifications` but not `msg_lifecycle_v1`, which
2.1.220 advertised. Experiment: a scripted test recording `system/init.capabilities` on 2.1.252.
