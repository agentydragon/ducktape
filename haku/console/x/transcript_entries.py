"""The neutral conversation vocabulary as `haku_conversations` hands it out.

<conversation_events.py> is what a conversation *is*, in dataclasses the console's own code folds
and renders. <../tools/conversations.py> is what an MCP caller reads, in Pydantic models with
discriminators and descriptions. This is the one place the two are the same conversation, so a
change to either shows up here rather than as a surface quietly drifting from the vocabulary it
claims to expose.

Two things happen on the way across.

**Deltas do not cross.** `TextDelta` is sub-message transport, and by the vocabulary's own
contract a message's deltas concatenate to exactly the `text` its `MessageCompleted` carries — so
on a conversation being read back they are the same prose twice. A reader that wants the typing
asks `read_rollout` for `stream_event` frames by name.

**What remains is numbered.** The index is the entry's position in the whole session's
transcript, and it is what `read_transcript`'s cursor names; see `TranscriptCursor` for why an
ordinal is a safe key for this one order.
"""

from __future__ import annotations

from haku.console.tools import conversations
from haku.console.x import conversation_events


def entries(projection: conversation_events.Projection) -> list[conversations.TranscriptEntry]:
    """One session's projection as the wire models, deltas dropped and the rest numbered."""
    return [
        _entry(event, index)
        for index, event in enumerate(
            event for event in projection.events if not isinstance(event, conversation_events.TextDelta)
        )
    ]


def unreadable(projection: conversation_events.Projection) -> dict[str, int] | None:
    """What the fold could not read, or nothing — never an empty map standing in for "none"."""
    return dict(projection.unprojected) or None


def _entry(event: conversation_events.ConversationEvent, index: int) -> conversations.TranscriptEntry:
    provenance = _provenance(event.provenance)
    match event:
        case conversation_events.MessageCompleted():
            return conversations.MessageEntry(
                index=index,
                provenance=provenance,
                message=_message(event.message),
                text=event.text,
                agent_message_id=event.agent_message_id,
            )
        case conversation_events.Reasoning():
            return conversations.ReasoningEntry(
                index=index, provenance=provenance, message=_message(event.message), summary=event.summary
            )
        case conversation_events.ToolCallStarted():
            return conversations.ToolCallEntry(
                index=index,
                provenance=provenance,
                message=_message(event.message),
                call_id=event.call_id,
                tool_name=event.tool_name,
                arguments=dict(event.arguments),
            )
        case conversation_events.ToolCallCompleted():
            return conversations.ToolResultEntry(
                index=index,
                provenance=provenance,
                call_id=event.call_id,
                content=_content(event.content),
                structured=event.structured,
                outcome=conversations.Outcome(event.outcome),
            )
        case conversation_events.ActivityStarted():
            return conversations.ActivityStartedEntry(
                index=index, provenance=provenance, activity_id=event.activity_id, description=event.description
            )
        case conversation_events.ActivityCompleted():
            return conversations.ActivityFinishedEntry(
                index=index,
                provenance=provenance,
                activity_id=event.activity_id,
                summary=event.summary,
                outcome=conversations.Outcome(event.outcome),
            )
        case conversation_events.TurnCompleted():
            return conversations.TurnEndEntry(
                index=index, provenance=provenance, outcome=event.outcome, usage=_usage(event.usage)
            )
        # `TextDelta` is the remaining member of the union and never reaches here; a member added
        # to the vocabulary without a case lands on this line rather than being dropped in silence.
        case _:
            raise ValueError(f"no transcript entry for {event=}")


def _message(key: conversation_events.MessageKey) -> conversations.MessageRef:
    return conversations.MessageRef(opened_at_frame_seq=key.opened_at_frame_seq)


def _provenance(provenance: conversation_events.Provenance) -> conversations.EntryProvenance:
    match provenance:
        case conversation_events.FrameRange():
            return conversations.FromFrames(
                first_frame_seq=provenance.first_frame_seq, last_frame_seq=provenance.last_frame_seq
            )
        case conversation_events.Authored():
            return conversations.ConsoleAuthored()


def _content(content: conversation_events.ToolResultContent) -> conversations.ResultContent:
    match content:
        case conversation_events.TextContent():
            return conversations.ResultText(text=content.text)
        case conversation_events.ToolReferences():
            return conversations.ResultToolReferences(tool_names=list(content.tool_names))
        case conversation_events.OpaqueContent():
            return conversations.ResultOpaque(payload=content.payload)


def _usage(usage: conversation_events.Usage | None) -> conversations.TurnUsage | None:
    if usage is None:
        return None
    return conversations.TurnUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        cost_usd=usage.cost_usd,
        duration_ms=usage.duration_ms,
    )
