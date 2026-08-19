"""The neutral conversation vocabulary as `haku_conversations` hands it out.

<conversation_events.py> is what a conversation *is*, in dataclasses the console's own code folds
and renders; <conversation_records.py> is what a read hands back, in Pydantic models
<../tools/conversations.py> serialises. This is the one place the two are the same conversation, so
a change to either shows up here rather than as a surface quietly drifting from the vocabulary it
claims to expose.

**Segments are folded here rather than handed on.** The vocabulary emits prose as increments so a
live channel can print them as they arrive; a transcript is read after the fact and wants the item,
whose text is the concatenation of exactly those increments.

**An item that never completed is not an entry.** A turn that died mid-message left prose nothing
finished saying, and a transcript that printed it would report a half-sentence as what was said.

**What remains is numbered.** The index is the entry's position in the whole session's transcript
and is what `read_transcript`'s cursor names; see `TranscriptCursor` for why an ordinal is a safe
key for this one order.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from haku.console.chat_models import ItemType, ReasoningDisclosure
from haku.console.x import conversation_events, conversation_records


@dataclass(slots=True)
class _Open:
    """An item the fold has seen the start of, and the prose that has arrived for it."""

    segments: list[str] = field(default_factory=list)

    def text(self) -> str:
        return "".join(self.segments)


@dataclass(slots=True)
class _Items:
    """What a segment can be addressed by, mirroring the store's own two ways.

    A tool call is found by the id its protocol supplies, because its answer arrives long after its
    ask; everything else is the open item of its type, because a backend writes one at a time.
    """

    by_type: dict[ItemType, _Open] = field(default_factory=dict)
    by_call: dict[str, _Open] = field(default_factory=dict)

    def open(self, item_type: ItemType, *, call_id: str | None = None) -> None:
        item = self.by_type[item_type] = _Open()
        if call_id is not None:
            self.by_call[call_id] = item

    def named(self, ref: conversation_events.ItemRef) -> _Open | None:
        match ref:
            case conversation_events.CallRef(call_id=call_id):
                return self.by_call.get(call_id)
            case conversation_events.OpenRef(item_type=item_type):
                return self.by_type.get(item_type)

    def close(self, item_type: ItemType, *, call_id: str | None = None) -> _Open | None:
        closed = self.by_call.pop(call_id, None) if call_id is not None else self.by_type.get(item_type)
        self.by_type.pop(item_type, None)
        return closed


def entries(projection: conversation_events.Projection) -> list[conversation_records.TranscriptEntry]:
    """One session's projection as the wire models: one entry per finished item, then numbered."""
    open_items = _Items()
    said: list[conversation_records.TranscriptEntry] = []
    for event in projection.events:
        if (entry := _entry(event, open_items, index=len(said))) is not None:
            said.append(entry)
    return said


def unreadable(projection: conversation_events.Projection) -> dict[str, int] | None:
    """What the fold could not read, or nothing — never an empty map standing in for "none"."""
    return dict(projection.unprojected) or None


def _entry(
    event: conversation_events.ConversationEvent, open_items: _Items, *, index: int
) -> conversation_records.TranscriptEntry | None:
    """This event's entry, where it produces one, folding what it says into the item it names."""
    provenance = _provenance(event.provenance)
    match event:
        case conversation_events.MessageStarted():
            open_items.open(ItemType.MESSAGE)
            return None
        case conversation_events.ReasoningStarted():
            open_items.open(ItemType.REASONING)
            return None
        case conversation_events.ToolCallStarted():
            open_items.open(ItemType.TOOL_CALL, call_id=event.call_id)
            # The one item whose entry is written at its start: its arguments are whole by then, and
            # its answer is a separate entry joined by `call_id`.
            return conversation_records.ToolCallEntry(
                index=index,
                provenance=provenance,
                call_id=event.call_id,
                tool_name=event.tool_name,
                arguments=dict(event.arguments),
            )
        case conversation_events.ItemSegment():
            if (item := open_items.named(event.item)) is not None:
                item.segments.append(event.text)
            return None
        case conversation_events.MessageCompleted():
            if (message := open_items.close(ItemType.MESSAGE)) is None:
                return None
            return conversation_records.MessageEntry(
                index=index, provenance=provenance, text=message.text(), backend_item_id=event.backend_item_id
            )
        case conversation_events.ReasoningCompleted():
            if (reasoning := open_items.close(ItemType.REASONING)) is None:
                return None
            withheld = event.disclosure is ReasoningDisclosure.WITHHELD
            return conversation_records.ReasoningEntry(
                index=index, provenance=provenance, summary=None if withheld else reasoning.text()
            )
        case conversation_events.ToolCallCompleted():
            answered = open_items.close(ItemType.TOOL_CALL, call_id=event.item.call_id)
            return conversation_records.ToolResultEntry(
                index=index,
                provenance=provenance,
                call_id=event.item.call_id,
                content="" if answered is None else answered.text(),
                structured=event.structured,
                outcome=conversation_records.Outcome(event.outcome),
            )
        case conversation_events.TurnCompleted():
            return conversation_records.TurnEndEntry(index=index, provenance=provenance, outcome=event.outcome)


def _provenance(provenance: conversation_events.Provenance) -> conversation_records.EntryProvenance:
    match provenance:
        case conversation_events.FrameRange():
            return conversation_records.FromFrames(
                first_frame_seq=provenance.first_frame_seq, last_frame_seq=provenance.last_frame_seq
            )
        case conversation_events.Authored():
            return conversation_records.ConsoleAuthored()
