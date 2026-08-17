// Reading the raw frame log: which frames to ask the API for, how a fetched page joins the ones
// already loaded, and the one line that says what a frame is without expanding its JSON. All pure,
// so the decisions are unit-tested; the page component (session_frames_page.tsx) owns the fetching
// and the DOM.
import type { SessionFrame, SessionFramePage } from "../client";

/** What the inspector is showing. `frames` is the log as a reader means it — everything the CLI and
 * the bridge said; `deltas` is the token-batch stream, which a turn produces in the hundreds and
 * whose content the completed `assistant` frame repeats. Deltas are their own mode rather than a
 * checkbox that adds them, because interleaved they bury the frames they duplicate, and they answer
 * one question: how far an answer got before it was cut off. */
export type FrameMode = "frames" | "deltas";

const DELTA_KIND = "stream_event";

/** The `kind` filter for a mode. `undefined` means "no filter", which the API reads as everything
 * except the deltas — so the default view needs no closed list of kinds and a frame type this
 * release has never heard of still shows up. */
export function kindsForMode(mode: FrameMode): string[] | undefined {
  return mode === "deltas" ? [DELTA_KIND] : undefined;
}

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
    next_before_seq: page.next_before_seq,
  };
}

/** What the adapter's fold read nothing from in this frame, in one line, or "" when it read it all.
 *
 * The keys are the backend's own frame class names, shown verbatim: this is the debug surface, and
 * "system/vcs_state_changed" is the string to go and add a branch for. A count rides along only
 * when a frame carried the same unread class more than once, which is a batched assistant frame. */
export function unprojectedSummary(frame: SessionFrame): string {
  return Object.entries(frame.unprojected ?? {})
    .map(([kind, count]) => (count > 1 ? `${kind} ×${count}` : kind))
    .join(" · ");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function field(payload: Record<string, unknown>, ...path: string[]): unknown {
  let value: unknown = payload;
  for (const key of path) {
    if (!isRecord(value)) return undefined;
    value = value[key];
  }
  return value;
}

/** One line of `value`, collapsed and cut — a summary never wraps the row it labels. */
function oneLine(value: unknown, limit = 140): string {
  if (typeof value !== "string") return "";
  const text = value.replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

// Content blocks named individually, before the "+N more" tail. Two is enough to tell a text
// answer from a batch of tool calls, which is the whole question a summary answers.
const NAMED_BLOCKS = 2;

function blockSummary(block: Record<string, unknown>): string {
  switch (block.type) {
    case "text":
      return oneLine(block.text);
    case "thinking":
      return "thinking";
    case "tool_use":
      return typeof block.name === "string" ? `${block.name}()` : "tool_use";
    case "tool_result":
      return typeof block.tool_use_id === "string" ? `result → ${block.tool_use_id}` : "tool_result";
    default:
      return typeof block.type === "string" ? block.type : "";
  }
}

/** What this frame is, in one line, without opening its payload.
 *
 * Deliberately tolerant: the payload is the wire, where an unseen block type or `kind` is a new CLI
 * feature rather than a bug, and the frame's own JSON sits right below the summary. So an
 * unrecognised shape summarises to nothing instead of guessing. */
export function frameSummary(frame: SessionFrame): string {
  const { payload } = frame;
  if (frame.kind === "stream_event") return oneLine(field(payload, "event", "delta", "text"));
  if (frame.kind === "setup_output") return oneLine(payload.text);
  if (frame.kind === "result") {
    const subtype = typeof payload.subtype === "string" ? payload.subtype : "result";
    return payload.is_error === true ? `${subtype} · error` : subtype;
  }
  const content = field(payload, "message", "content");
  if (typeof content === "string") return oneLine(content);
  if (Array.isArray(content)) {
    const blocks = content.filter(isRecord);
    const named = blocks.slice(0, NAMED_BLOCKS).map(blockSummary).filter(Boolean);
    const rest = blocks.length - NAMED_BLOCKS;
    return [...named, ...(rest > 0 ? [`+${rest} more`] : [])].join(" · ");
  }
  return oneLine(payload.subtype);
}
