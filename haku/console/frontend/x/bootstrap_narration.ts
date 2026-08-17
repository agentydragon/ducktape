import type { ConversationSession } from "../client";

export type NarrationLine = ConversationSession["narration"][number];

export type BootstrapNarration = {
  lines: NarrationLine[];
  /** Whether the panel opens showing every line rather than a one-line summary. */
  startsExpanded: boolean;
};

/** What the sandbox said while coming up, ordered, and how prominently to show it.
 *
 * `null` when the session narrated nothing: an empty panel is worse than no panel.
 *
 * **Ordered by `frame_seq`, and never deduplicated.** These rows carry no frame identity — the
 * runner sends them `replayable=False` — so the sequence is the only order there is, and two
 * identical texts are two things that happened. Sorting here rather than trusting the response
 * keeps the rule local to the component that depends on it.
 *
 * It opens **expanded when nothing else is happening yet**: while the session is still
 * provisioning, and whenever the transcript is empty — including the session that died during
 * setup, where this is the entire account of what happened. Once there are messages the transcript
 * is what the operator came for, so the narration collapses out of its way.
 */
export function bootstrapNarration(
  session: Pick<ConversationSession, "status" | "narration" | "messages">
): BootstrapNarration | null {
  if (session.narration.length === 0) return null;
  return {
    lines: [...session.narration].sort((left, right) => left.frame_seq - right.frame_seq),
    startsExpanded: session.status === "provisioning" || session.messages.length === 0,
  };
}
