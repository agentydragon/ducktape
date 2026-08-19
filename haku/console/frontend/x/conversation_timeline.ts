import type { ConversationItem, ConversationSession } from "../client";

export type ConversationTurn = ConversationSession["turns"][number];

export type ConversationTimelineEntry =
  | { kind: "item"; item: ConversationItem }
  | { kind: "turn"; turn: ConversationTurn; number: number };

/** Interleave a conversation's turn boundaries into its transcript, in item order.
 *
 * The read API carries no turn→item link, so a boundary is placed by the one relation the two do
 * share: a turn's `started_at` against each item's `created_at`. A turn therefore sits immediately
 * before the first item created at or after it began — the transcript position where that exchange
 * started — and a turn that began after the last recorded item trails the transcript. Instants are
 * parsed rather than compared as strings, because Pydantic emits the fractional second only when it
 * is non-zero and `"…:10.5Z" < "…:10Z"` lexically.
 *
 * Turns are sorted here rather than trusted: the endpoint returns them newest-first, which is
 * the opposite of the transcript they are numbered against.
 */
export function conversationTimeline(
  items: readonly ConversationItem[],
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
  for (const item of items) {
    emitTurnsStartedBy(Date.parse(item.created_at));
    entries.push({ kind: "item", item });
  }
  emitTurnsStartedBy(Number.POSITIVE_INFINITY);
  return entries;
}
