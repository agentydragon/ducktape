/**
 * What a status looks like, wherever it is drawn: a thread's or a Sandbox's indicator in the page, and
 * a thread's mark in the tab favicon. This is the only place that decides a status's shape and color;
 * mark_glyph.tsx and thread_favicon.ts only draw what these tables say.
 */

/**
 * `running`: a turn is in flight. `idle`: the harness is live and waiting for input. `failed`: the
 * runner feed failed. `inactive`: nothing live to show (archived, Sandbox suspended or gone, harness
 * stopped, feed ended, or not confirmed).
 */
export type ThreadStatusKind = "running" | "idle" | "failed" | "inactive";

/**
 * `ready`: the Pod is up and can take sessions. `pending`: on its way (grants applying, no Pod yet,
 * Pod not ready). `suspended`: deliberately off. `failed`: a grant error or a failed Pod. `gone`:
 * deleted, deleting, or a Pod that has ended.
 */
export type SandboxStatusKind = "ready" | "pending" | "suspended" | "failed" | "gone";

/** The shapes a thread's mark can take, in the page and in the favicon alike. */
export type ThreadMarkShape = "dot" | "chevrons";
export type MarkShape = ThreadMarkShape | "play" | "pause" | "clock" | "cross";

export interface StatusMark<Shape extends MarkShape = MarkShape> {
  readonly shape: Shape;
  readonly color: string;
}

// Mantine default-palette shades. Blue means live and waiting, for a thread and a Sandbox alike, and
// reads apart from both green (a turn running) and gray (switched off or gone).
const GREEN = "#40c057"; // green-6
const BLUE = "#4dabf7"; // blue-4
const YELLOW = "#fab005"; // yellow-6
const RED = "#fa5252"; // red-6
const GRAY = "#868e96"; // gray-6

/**
 * A thread's mark, plus the glyph its browser tab title leads with, where there is no color to draw
 * with. Plain text characters only: ones a platform may draw as emoji (▶, ✕) would not follow the tab's
 * text color. Filled means live and hollow means not live.
 */
export interface ThreadStatusMark extends StatusMark<ThreadMarkShape> {
  readonly glyph: string;
}

export const THREAD_STATUS_MARKS: Record<ThreadStatusKind, ThreadStatusMark> = {
  running: { shape: "chevrons", color: GREEN, glyph: "»" },
  idle: { shape: "dot", color: BLUE, glyph: "●" },
  failed: { shape: "dot", color: RED, glyph: "×" },
  inactive: { shape: "dot", color: GRAY, glyph: "○" },
};

export const SANDBOX_STATUS_MARKS: Record<SandboxStatusKind, StatusMark> = {
  ready: { shape: "play", color: BLUE },
  pending: { shape: "clock", color: YELLOW },
  suspended: { shape: "pause", color: GRAY },
  failed: { shape: "cross", color: RED },
  gone: { shape: "cross", color: GRAY },
};

/** Time for the in-page chevrons to advance one chevron pitch to the right. */
export const CHEVRON_CYCLE_MS = 1_000;
