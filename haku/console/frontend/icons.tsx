// Tabler icons via **per-icon subpath imports** — never `import { … } from "@tabler/icons-react"`
// (the barrel OOMs esbuild on RBE at ~8.7 GB). Types for the
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
import IconMessagePlus from "@tabler/icons-react/dist/esm/icons/IconMessagePlus.mjs";
import IconRepeat from "@tabler/icons-react/dist/esm/icons/IconRepeat.mjs";
import IconSettings from "@tabler/icons-react/dist/esm/icons/IconSettings.mjs";
import IconUnlink from "@tabler/icons-react/dist/esm/icons/IconUnlink.mjs";
import IconUsers from "@tabler/icons-react/dist/esm/icons/IconUsers.mjs";
import IconX from "@tabler/icons-react/dist/esm/icons/IconX.mjs";
import type { ComponentProps } from "react";

import { GMAIL_ICON_DATA_URI, GOOGLE_CALENDAR_ICON_DATA_URI } from "./brand_icon_data";

type TablerIconProps = ComponentProps<typeof IconChecklist>;

/** Checklist — the shell's approvals-queue toggle. */
export function ChecklistIcon(props: TablerIconProps): JSX.Element {
  return <IconChecklist size={20} {...props} />;
}

/** Home — selects the persistent Haku UI frame. */
export function HomeIcon(props: TablerIconProps): JSX.Element {
  return <IconHome size={20} {...props} />;
}

/** Check in a circle — approvals are connected and current. */
export function SyncCurrentIcon(props: TablerIconProps): JSX.Element {
  return <IconCircleCheck size={20} {...props} />;
}

/** Warning triangle — approvals sync is unhealthy. */
export function SyncErrorIcon(props: TablerIconProps): JSX.Element {
  return <IconAlertTriangle size={20} {...props} />;
}

/** Close — dismisses a shell drawer or popover. */
export function CloseIcon(props: TablerIconProps): JSX.Element {
  return <IconX size={20} {...props} />;
}

/** Clock-with-rewind — links to the past-tool-calls history view. */
export function HistoryIcon(props: TablerIconProps): JSX.Element {
  return <IconHistory size={20} {...props} />;
}

/** Gear — links to the settings view. */
export function SettingsIcon(props: TablerIconProps): JSX.Element {
  return <IconSettings size={20} {...props} />;
}

/** Unlink — disconnects an operator account from an MCP server. */
export function DisconnectIcon(props: TablerIconProps): JSX.Element {
  return <IconUnlink size={20} {...props} />;
}

/** List with detail — opens the operator's conversation inventory. */
export function ConversationsIcon(props: TablerIconProps): JSX.Element {
  return <IconListDetails size={20} {...props} />;
}

/** Message with plus — starts a new conversation from the conversation inventory. */
export function NewConversationIcon(props: TablerIconProps): JSX.Element {
  return <IconMessagePlus size={20} {...props} />;
}

/** Map pin — the shell's location-sharing control, and a preview's location field. */
export function MapPinIcon(props: TablerIconProps): JSX.Element {
  return <IconMapPin size={20} {...props} />;
}

/** Camera — the shell's screenshot-capture control. */
export function CameraIcon(props: TablerIconProps): JSX.Element {
  return <IconCamera size={20} {...props} />;
}

/** Clock — a calendar event's when/time field. */
export function ClockIcon(props: TablerIconProps): JSX.Element {
  return <IconClock size={20} {...props} />;
}

/** Bell — a calendar event's reminders field. */
export function BellIcon(props: TablerIconProps): JSX.Element {
  return <IconBell size={20} {...props} />;
}

/** People — a calendar event's attendees field. */
export function UsersIcon(props: TablerIconProps): JSX.Element {
  return <IconUsers size={20} {...props} />;
}

/** Calendar — a non-primary target calendar. */
export function CalendarIcon(props: TablerIconProps): JSX.Element {
  return <IconCalendarEvent size={20} {...props} />;
}

/** Repeat arrows — a calendar event's recurrence rule. */
export function RepeatIcon(props: TablerIconProps): JSX.Element {
  return <IconRepeat size={20} {...props} />;
}

/** Envelope — a Gmail draft's recipients / a thread list. */
export function MailIcon(props: TablerIconProps): JSX.Element {
  return <IconMail size={20} {...props} />;
}

/** Plain list — the "Brief" (compact) side of a card's detail toggle. */
export function ListIcon(props: TablerIconProps): JSX.Element {
  return <IconList size={20} {...props} />;
}

/** List with detail lines — the "Full" (detailed) side of a card's detail toggle. */
export function ListDetailsIcon(props: TablerIconProps): JSX.Element {
  return <IconListDetails size={20} {...props} />;
}

// Gmail's and Google Calendar's official multicolor app icons, which are not Tabler glyphs and so
// don't fit the wrapper pattern above. Their data URIs (brand_icon_data.ts) are generated at build
// time by BUILD.bazel's data_uri_module over the SVGs MODULE.bazel fetches, so the bundle stays
// self-contained with no runtime fetch to google.com. Used only to mark a link that opens in that
// app, where a generic mail/calendar glyph would be ambiguous.

/** Gmail's own multicolor icon — marks a link that opens Gmail, unlike `MailIcon` above. */
export function GmailIcon({ size = 20 }: { size?: number }): JSX.Element {
  return <img src={GMAIL_ICON_DATA_URI} alt="" width={size} height={size} style={{ display: "block" }} />;
}

/** Google Calendar's own multicolor icon — marks a link that opens Google Calendar, unlike
 * `CalendarIcon` above. */
export function GoogleCalendarIcon({ size = 20 }: { size?: number }): JSX.Element {
  return <img src={GOOGLE_CALENDAR_ICON_DATA_URI} alt="" width={size} height={size} style={{ display: "block" }} />;
}
