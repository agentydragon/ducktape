import type { ClaudeChatMessage, ConversationSession } from "../client";

export type ConversationTurn = ConversationSession["turns"][number];

export type ConversationTimelineEntry =
  | { kind: "message"; message: ClaudeChatMessage }
  | { kind: "turn"; turn: ConversationTurn; number: number };

/** Interleave a conversation's turn boundaries into its transcript, in message order.
 *
 * The read API carries no turn→message link, so a boundary is placed by the one relation the
 * two do share: a turn's `started_at` against each message's `created_at`. A turn therefore
 * sits immediately before the first message created at or after it began — the transcript
 * position where that exchange started — and a turn that began after the last recorded message
 * trails the transcript. Instants are parsed rather than compared as strings, because Pydantic
 * emits the fractional second only when it is non-zero and `"…:10.5Z" < "…:10Z"` lexically.
 *
 * Turns are sorted here rather than trusted: the endpoint returns them newest-first, which is
 * the opposite of the transcript they are numbered against.
 */
export function conversationTimeline(
  messages: readonly ClaudeChatMessage[],
  turns: readonly ConversationTurn[]
): ConversationTimelineEntry[] {
  const ordered = [...turns].sort((left, right) => Date.parse(left.started_at) - Date.parse(right.started_at));
  const entries: ConversationTimelineEntry[] = [];
  let pending = 0;
  const emitTurnsStartedBy = (instant: number) => {
    while (pending < ordered.length && Date.parse(ordered[pending].started_at) <= instant) {
      entries.push({ kind: "turn", turn: ordered[pending], number: pending + 1 });
      pending += 1;
    }
  };
  for (const message of messages) {
    emitTurnsStartedBy(Date.parse(message.created_at));
    entries.push({ kind: "message", message });
  }
  emitTurnsStartedBy(Number.POSITIVE_INFINITY);
  return entries;
}
