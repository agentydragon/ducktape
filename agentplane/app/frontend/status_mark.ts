/**
 * What a status looks like, wherever it is drawn: a thread's or a Sandbox's indicator in the page, and
 * a thread's mark in the tab favicon and title. This is the only place that decides a status's icon and
 * color; mark_glyph.tsx and thread_favicon.ts only draw what these tables say.
 */
import type { ComponentType } from "react";
// Per-icon subpaths, never the barrel: see tabler_icons.d.ts.
import IconAlertTriangle from "@tabler/icons-react/dist/esm/icons/IconAlertTriangle.mjs";
import IconArchive from "@tabler/icons-react/dist/esm/icons/IconArchive.mjs";
import IconCircleX from "@tabler/icons-react/dist/esm/icons/IconCircleX.mjs";
import IconClock from "@tabler/icons-react/dist/esm/icons/IconClock.mjs";
import IconPlayerPause from "@tabler/icons-react/dist/esm/icons/IconPlayerPause.mjs";
import IconPlayerPlay from "@tabler/icons-react/dist/esm/icons/IconPlayerPlay.mjs";
import IconPower from "@tabler/icons-react/dist/esm/icons/IconPower.mjs";

import { RunningChevrons, StatusDot } from "./custom_mark_icons";

/** Each kind's meaning is on its row of `THREAD_STATUS_MARKS`. */
export type ThreadStatusKind =
  "running" | "idle" | "turn_error" | "stopped" | "suspended" | "failed" | "archived" | "inactive";

/** Each kind's meaning is on its row of `SANDBOX_STATUS_MARKS`. */
export type SandboxStatusKind = "ready" | "pending" | "suspended" | "failed" | "gone";

/** An icon the page draws: a library icon, or one of custom_mark_icons.tsx, sized by the CSS around it. */
export type MarkIcon = ComponentType;

export interface StatusMark {
  readonly icon: MarkIcon;
  readonly color: string;
}

/** What the favicon hand-draws for a thread's status; thread_favicon.ts owns the art. */
export type FaviconShape = "dot" | "chevrons" | "alert" | "power" | "pause" | "archive";

// Mantine default-palette shades. Blue means live and waiting, for a thread and a Sandbox alike, and
// reads apart from both green (a turn running) and gray (switched off or gone).
const GREEN = "#40c057"; // green-6
const BLUE = "#4dabf7"; // blue-4
const YELLOW = "#fab005"; // yellow-6
const RED = "#fa5252"; // red-6
const GRAY = "#868e96"; // gray-6

/**
 * A thread's mark, plus what the surfaces that cannot draw `icon` use instead: the favicon's shape,
 * and the glyph its browser tab title leads with, where there is no color to draw with. The glyph is
 * a plain text character: ones a platform may draw as emoji (▶, ✕) would not follow the tab's text
 * color. Filled means live and hollow means not live.
 */
export interface ThreadStatusMark extends StatusMark {
  readonly favicon: FaviconShape;
  readonly glyph: string;
}

export const THREAD_STATUS_MARKS: Record<ThreadStatusKind, ThreadStatusMark> = {
  // A turn is in flight.
  running: { icon: RunningChevrons, color: GREEN, favicon: "chevrons", glyph: "»" },
  // The harness is live and waiting for input.
  idle: { icon: StatusDot, color: BLUE, favicon: "dot", glyph: "●" },
  // As `idle`, but the last turn to complete ended in an error; the next clean one makes it `idle`.
  // Red like `failed`, but a triangle where that is a dot: a failed runner feed is the infrastructure, this
  // is what the last turn did. "!" is plain text, where ⚠ may be drawn as emoji.
  turn_error: { icon: IconAlertTriangle, color: RED, favicon: "alert", glyph: "!" },
  // The harness is down (shut down or lost), whether its runner feed is still attached or has ended.
  stopped: { icon: IconPower, color: GRAY, favicon: "power", glyph: "⏻" },
  // The owning Sandbox is suspended; the native Session and its history remain available.
  suspended: { icon: IconPlayerPause, color: GRAY, favicon: "pause", glyph: "Ⅱ" },
  // The runner feed failed.
  failed: { icon: StatusDot, color: RED, favicon: "dot", glyph: "×" },
  // Archived, whatever its retained history says. An arrow onto a bar reads as filing away at tab-title size; box shapes read as a missing-glyph box
  // there.
  archived: { icon: IconArchive, color: GRAY, favicon: "archive", glyph: "↧" },
  // Nothing else live to show: the Sandbox is gone, setup or the harness's first launch is still
  // running, or the feed ended with the harness not known to be down, or not confirmed.
  inactive: { icon: StatusDot, color: GRAY, favicon: "dot", glyph: "○" },
};

export const SANDBOX_STATUS_MARKS: Record<SandboxStatusKind, StatusMark> = {
  // The Pod is up and can take sessions.
  ready: { icon: IconPlayerPlay, color: BLUE },
  // On its way: grants applying, no Pod yet, or Pod not ready.
  pending: { icon: IconClock, color: YELLOW },
  // Deliberately off.
  suspended: { icon: IconPlayerPause, color: GRAY },
  // A grant error or a failed Pod.
  failed: { icon: IconCircleX, color: RED },
  // Deleted, deleting, or a Pod that has ended.
  gone: { icon: IconCircleX, color: GRAY },
};
