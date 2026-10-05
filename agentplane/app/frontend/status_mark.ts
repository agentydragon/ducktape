/**
 * What a status looks like, wherever it is drawn: a thread's or a Sandbox's indicator in the page, and
 * a thread's mark in the tab favicon and title. This is the only place that decides a status's icon and
 * color; mark_glyph.tsx and thread_favicon.ts only draw what these tables say.
 */
import type { ComponentType } from "react";
// Per-icon subpaths, never the barrel: see tabler_icons.d.ts.
import IconArchive from "@tabler/icons-react/dist/esm/icons/IconArchive.mjs";
import IconCircleX from "@tabler/icons-react/dist/esm/icons/IconCircleX.mjs";
import IconClock from "@tabler/icons-react/dist/esm/icons/IconClock.mjs";
import IconPlayerPause from "@tabler/icons-react/dist/esm/icons/IconPlayerPause.mjs";
import IconPlayerPlay from "@tabler/icons-react/dist/esm/icons/IconPlayerPlay.mjs";
import IconPower from "@tabler/icons-react/dist/esm/icons/IconPower.mjs";

import { RunningChevrons, StatusDot } from "./custom_mark_icons";

/**
 * `running`: a turn is in flight. `idle`: the harness is live and waiting for input. `stopped`: the
 * runner feed is attached but the harness is down (shut down or lost). `failed`: the runner feed
 * failed. `archived`: the thread was archived, whatever its retained history says. `inactive`: nothing
 * else live to show (Sandbox suspended or gone, feed ended, or not confirmed).
 */
export type ThreadStatusKind = "running" | "idle" | "stopped" | "failed" | "archived" | "inactive";

/**
 * `ready`: the Pod is up and can take sessions. `pending`: on its way (grants applying, no Pod yet,
 * Pod not ready). `suspended`: deliberately off. `failed`: a grant error or a failed Pod. `gone`:
 * deleted, deleting, or a Pod that has ended.
 */
export type SandboxStatusKind = "ready" | "pending" | "suspended" | "failed" | "gone";

/** An icon the page draws: a library icon, or one of custom_mark_icons.tsx, sized by the CSS around it. */
export type MarkIcon = ComponentType;

export interface StatusMark {
  readonly icon: MarkIcon;
  readonly color: string;
}

/** What the favicon hand-draws for a thread's status; thread_favicon.ts owns the art. */
export type FaviconShape = "dot" | "chevrons" | "power" | "archive";

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
  running: { icon: RunningChevrons, color: GREEN, favicon: "chevrons", glyph: "»" },
  idle: { icon: StatusDot, color: BLUE, favicon: "dot", glyph: "●" },
  stopped: { icon: IconPower, color: GRAY, favicon: "power", glyph: "⏻" },
  failed: { icon: StatusDot, color: RED, favicon: "dot", glyph: "×" },
  // A box with drawer lines reads as an archive box at tab-title size, where a plain square reads as a
  // missing-glyph box.
  archived: { icon: IconArchive, color: GRAY, favicon: "archive", glyph: "▤" },
  inactive: { icon: StatusDot, color: GRAY, favicon: "dot", glyph: "○" },
};

export const SANDBOX_STATUS_MARKS: Record<SandboxStatusKind, StatusMark> = {
  ready: { icon: IconPlayerPlay, color: BLUE },
  pending: { icon: IconClock, color: YELLOW },
  suspended: { icon: IconPlayerPause, color: GRAY },
  failed: { icon: IconCircleX, color: RED },
  gone: { icon: IconCircleX, color: GRAY },
};
