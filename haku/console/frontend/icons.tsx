// Tabler icons via **per-icon subpath imports** — never `import { … } from "@tabler/icons-react"`
// (the barrel OOMs esbuild on RBE at ~8.7 GB). Types for the
// `.mjs` subpaths come from the ambient declaration in `tabler_icons.d.ts`. Shell icons default
// to 20px; renderer-only icons are re-exported directly and sized by their callers.

import IconCamera from "@tabler/icons-react/dist/esm/icons/IconCamera.mjs";
import IconChecklist from "@tabler/icons-react/dist/esm/icons/IconChecklist.mjs";
import IconClock from "@tabler/icons-react/dist/esm/icons/IconClock.mjs";
import IconHome from "@tabler/icons-react/dist/esm/icons/IconHome.mjs";
import IconMapPin from "@tabler/icons-react/dist/esm/icons/IconMapPin.mjs";
import IconSettings from "@tabler/icons-react/dist/esm/icons/IconSettings.mjs";
import IconX from "@tabler/icons-react/dist/esm/icons/IconX.mjs";
import type { JSX, ComponentProps } from "react";

import { GMAIL_ICON_DATA_URI, GOOGLE_CALENDAR_ICON_DATA_URI } from "./brand_icon_data";

export { default as SyncErrorIcon } from "@tabler/icons-react/dist/esm/icons/IconAlertTriangle.mjs";
export { default as SyncCurrentIcon } from "@tabler/icons-react/dist/esm/icons/IconCircleCheck.mjs";
export { default as BellIcon } from "@tabler/icons-react/dist/esm/icons/IconBell.mjs";
export { default as UsersIcon } from "@tabler/icons-react/dist/esm/icons/IconUsers.mjs";
export { default as CalendarIcon } from "@tabler/icons-react/dist/esm/icons/IconCalendarEvent.mjs";
export { default as RepeatIcon } from "@tabler/icons-react/dist/esm/icons/IconRepeat.mjs";
export { default as MailIcon } from "@tabler/icons-react/dist/esm/icons/IconMail.mjs";
export { default as ListIcon } from "@tabler/icons-react/dist/esm/icons/IconList.mjs";
export { default as ListDetailsIcon } from "@tabler/icons-react/dist/esm/icons/IconListDetails.mjs";

type TablerIconProps = ComponentProps<typeof IconChecklist>;

/** Checklist — the shell's capability-request toggle. */
export function ChecklistIcon(props: TablerIconProps): JSX.Element {
  return <IconChecklist size={20} {...props} />;
}

/** Home — selects the persistent Haku UI frame. */
export function HomeIcon(props: TablerIconProps): JSX.Element {
  return <IconHome size={20} {...props} />;
}

/** Close — dismisses a shell drawer or popover. */
export function CloseIcon(props: TablerIconProps): JSX.Element {
  return <IconX size={20} {...props} />;
}

/** Gear — links to the settings view. */
export function SettingsIcon(props: TablerIconProps): JSX.Element {
  return <IconSettings size={20} {...props} />;
}

/** Map pin — the shell's location-sharing control, and a preview's location field. */
export function MapPinIcon(props: TablerIconProps): JSX.Element {
  return <IconMapPin size={20} {...props} />;
}

/** Camera — the shell's screenshot-capture control. */
export function CameraIcon(props: TablerIconProps): JSX.Element {
  return <IconCamera size={20} {...props} />;
}

/** Clock — the shell's session-expiry control, and a calendar event's when/time field. */
export function ClockIcon(props: TablerIconProps): JSX.Element {
  return <IconClock size={20} {...props} />;
}

// Gmail's and Google Calendar's official multicolor app icons, which are not Tabler glyphs and so
// do not come from Tabler. Their data URIs (brand_icon_data.ts) are generated at build
// time by BUILD.bazel's data_uri_module over the SVGs MODULE.bazel fetches, so the bundle stays
// self-contained with no runtime fetch to google.com. Used only to mark a link that opens in that
// app, where a generic mail/calendar glyph would be ambiguous.

/** Gmail's own multicolor icon — marks a link that opens Gmail, unlike the generic mail glyph. */
export function GmailIcon({ size = 20 }: { size?: number }): JSX.Element {
  return <img src={GMAIL_ICON_DATA_URI} alt="" width={size} height={size} style={{ display: "block" }} />;
}

/** Google Calendar's own multicolor icon — marks a link that opens Google Calendar, unlike
 * the generic calendar glyph. */
export function GoogleCalendarIcon({ size = 20 }: { size?: number }): JSX.Element {
  return <img src={GOOGLE_CALENDAR_ICON_DATA_URI} alt="" width={size} height={size} style={{ display: "block" }} />;
}
