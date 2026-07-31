// Tabler icons via **per-icon subpath imports** — never `import { … } from "@tabler/icons-react"`
// (the barrel OOMs esbuild on RBE at ~8.7 GB; see debug/esbuild_tabler_memory.md). Types for the
// `.mjs` subpaths come from the ambient declaration in `tabler_icons.d.ts`. Thin wrappers keep a
// stable local name + a consistent glyph size; callers can still override via props.
import IconAlertTriangle from "@tabler/icons-react/dist/esm/icons/IconAlertTriangle.mjs";
import IconBell from "@tabler/icons-react/dist/esm/icons/IconBell.mjs";
import IconCalendarEvent from "@tabler/icons-react/dist/esm/icons/IconCalendarEvent.mjs";
import IconCamera from "@tabler/icons-react/dist/esm/icons/IconCamera.mjs";
import IconChecklist from "@tabler/icons-react/dist/esm/icons/IconChecklist.mjs";
import IconCircleCheck from "@tabler/icons-react/dist/esm/icons/IconCircleCheck.mjs";
import IconClock from "@tabler/icons-react/dist/esm/icons/IconClock.mjs";
import IconHistory from "@tabler/icons-react/dist/esm/icons/IconHistory.mjs";
import IconHome from "@tabler/icons-react/dist/esm/icons/IconHome.mjs";
import IconList from "@tabler/icons-react/dist/esm/icons/IconList.mjs";
import IconListDetails from "@tabler/icons-react/dist/esm/icons/IconListDetails.mjs";
import IconMail from "@tabler/icons-react/dist/esm/icons/IconMail.mjs";
import IconMapPin from "@tabler/icons-react/dist/esm/icons/IconMapPin.mjs";
import IconRepeat from "@tabler/icons-react/dist/esm/icons/IconRepeat.mjs";
import IconSettings from "@tabler/icons-react/dist/esm/icons/IconSettings.mjs";
import IconUsers from "@tabler/icons-react/dist/esm/icons/IconUsers.mjs";
import IconX from "@tabler/icons-react/dist/esm/icons/IconX.mjs";
import type { ComponentProps } from "react";

import { GMAIL_ICON_DATA_URI, GOOGLE_CALENDAR_ICON_DATA_URI } from "./brand_icon_data";

type TablerIconProps = ComponentProps<typeof IconChecklist>;

/** Checklist — the shell's approvals-queue toggle. */
export function ChecklistIcon(props: TablerIconProps) {
  return <IconChecklist size={20} {...props} />;
}

/** Home — selects the persistent Haku UI frame. */
export function HomeIcon(props: TablerIconProps) {
  return <IconHome size={20} {...props} />;
}

/** Check in a circle — approvals are connected and current. */
export function SyncCurrentIcon(props: TablerIconProps) {
  return <IconCircleCheck size={20} {...props} />;
}

/** Warning triangle — approvals sync is unhealthy. */
export function SyncErrorIcon(props: TablerIconProps) {
  return <IconAlertTriangle size={20} {...props} />;
}

/** Close — dismisses a shell drawer or popover. */
export function CloseIcon(props: TablerIconProps) {
  return <IconX size={20} {...props} />;
}

/** Clock-with-rewind — links to the past-tool-calls history view. */
export function HistoryIcon(props: TablerIconProps) {
  return <IconHistory size={20} {...props} />;
}

/** Gear — links to the settings view. */
export function SettingsIcon(props: TablerIconProps) {
  return <IconSettings size={20} {...props} />;
}

/** Map pin — the shell's location-sharing control, and a preview's location field. */
export function MapPinIcon(props: TablerIconProps) {
  return <IconMapPin size={20} {...props} />;
}

/** Camera — the shell's screenshot-capture control. */
export function CameraIcon(props: TablerIconProps) {
  return <IconCamera size={20} {...props} />;
}

/** Clock — a calendar event's when/time field. */
export function ClockIcon(props: TablerIconProps) {
  return <IconClock size={20} {...props} />;
}

/** Bell — a calendar event's reminders field. */
export function BellIcon(props: TablerIconProps) {
  return <IconBell size={20} {...props} />;
}

/** People — a calendar event's attendees field. */
export function UsersIcon(props: TablerIconProps) {
  return <IconUsers size={20} {...props} />;
}

/** Calendar — a non-primary target calendar. */
export function CalendarIcon(props: TablerIconProps) {
  return <IconCalendarEvent size={20} {...props} />;
}

/** Repeat arrows — a calendar event's recurrence rule. */
export function RepeatIcon(props: TablerIconProps) {
  return <IconRepeat size={20} {...props} />;
}

/** Envelope — a Gmail draft's recipients / a thread list. */
export function MailIcon(props: TablerIconProps) {
  return <IconMail size={20} {...props} />;
}

/** Plain list — the "Brief" (compact) side of a card's detail toggle. */
export function ListIcon(props: TablerIconProps) {
  return <IconList size={20} {...props} />;
}

/** List with detail lines — the "Full" (detailed) side of a card's detail toggle. */
export function ListDetailsIcon(props: TablerIconProps) {
  return <IconListDetails size={20} {...props} />;
}

// Gmail's and Google Calendar's official multicolor app icons — not a Tabler glyph, so they
// don't fit the wrapper pattern above. The data URI constants (imported above, from
// brand_icon_data.ts) are generated at build time (BUILD.bazel's data_uri_module, over the SVGs
// MODULE.bazel fetches as gmail_icon_svg / google_calendar_icon_svg) rather than pasted in as
// source, so the bundle stays self-contained with no runtime fetch to google.com. Used only to
// mark a link that opens in that app — the real brand mark reads unambiguously where a generic
// mail/calendar glyph (MailIcon/CalendarIcon above) would not.

/** Gmail's own multicolor icon — marks a link that opens Gmail (distinct from `MailIcon`
 * above, which is the generic envelope glyph used for in-app mail-related fields). */
export function GmailIcon({ size = 20 }: { size?: number }) {
  return <img src={GMAIL_ICON_DATA_URI} alt="" width={size} height={size} style={{ display: "block" }} />;
}

/** Google Calendar's own multicolor icon — marks a link that opens Google Calendar (distinct
 * from `CalendarIcon` above, the generic glyph used for in-app calendar-related fields). */
export function GoogleCalendarIcon({ size = 20 }: { size?: number }) {
  return <img src={GOOGLE_CALENDAR_ICON_DATA_URI} alt="" width={size} height={size} style={{ display: "block" }} />;
}
