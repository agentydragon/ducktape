# Empty message boxes in the SPA conversation view

The conversation page shows many message boxes with no content at all — the placeholder
`MessageView` renders when a `complete` message has empty `content` and no tool calls
(<../frontend/x/conversations_page.tsx>). It is common enough to be the page's dominant visual, so
whatever produces it is not a rare edge.

**A defect, not a missing feature.** Nobody has looked yet; the three readings below are ordered by
prior, not by evidence, and the first thing this note wants is which one it is.

## 1. A regression from the page moving onto the follow socket

#4353 replaced the conversation page's refetch with a subscription, and the client half is a merge:
`followed` in <../frontend/x/conversation_follow.ts> replaces what it holds on a `snapshot` and
merges rows by `message_id` on an `update`. **A snapshot arriving where an update should merge
produces exactly this symptom**, and so does a merge whose arriving row is thinner than the row it
replaces — `merged` swaps whole rows, so a `SessionMessageView` built without its tool-call join
overwrites one that had the calls.

Both ends are worth reading together: `ConversationUpdate.messages` in <../x/session_views.py> is
"the messages that moved", and <../x/conversation_follow.py> decides when a position can no longer
be served and a snapshot is sent instead.

## 2. Not every conversation event kind is implemented for render

The page renders `session_messages.content` plus the tool calls joined onto it, and nothing else.
The neutral vocabulary carries more than that: `Reasoning` (<../x/conversation_events.py>) is a
state precisely because "many real messages are thinking and nothing else, and a transcript
modelling only text renders them blank". The MCP read path has a `ReasoningEntry` for it
(<../x/transcript_entries.py>); the SPA has no branch. A thinking-only message therefore renders as
this placeholder by construction, which makes it the cheapest thing to rule in or out first.

## 3. A layer leak

<../docs/chat_layers.md> requires that a fact a channel shows exists conversation-side first, and
a browser tab is a channel. A message whose substance never became a row — because its frames
landed in `Projection.unprojected`, or because the fact lived only in the stack frame that noticed
it — has nothing for the tab to render however correct the transport is. This reading is last
because it predicts empty boxes only for the specific frames the fold does not cover, not for the
bulk of a transcript.
