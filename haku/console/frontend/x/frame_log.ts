// Reading the raw frame log: how a fetched page joins the ones already loaded. The payload is
// intentionally uninterpreted JSON; provider-specific summaries belong in provider tooling, not in
// the generic Console frontend.
import type { SessionFramePage } from "../client";

/** A page of earlier frames placed above what is loaded, dropping any frame present in both.
 *
 * The log is append-only, so a `before_seq` cursor cannot skip or repeat — but a page can still be
 * fetched twice (a double-clicked button), and splicing it in unfiltered would show one frame as
 * two. The cursor to walk further back comes from the fetched page; the frames already loaded are
 * newer than all of it. */
export function prependEarlierPage(page: SessionFramePage, loaded: SessionFramePage | null): SessionFramePage {
  if (!loaded) return page;
  const known = new Set(loaded.frames.map((frame) => frame.frame_seq));
  const earlier = page.frames.filter((frame) => !known.has(frame.frame_seq));
  return {
    frames: [...earlier, ...loaded.frames],
    conversation_id: loaded.conversation_id,
    runtime_kind: loaded.runtime_kind,
    harness_kind: loaded.harness_kind,
    next_before_seq: page.next_before_seq,
  };
}
