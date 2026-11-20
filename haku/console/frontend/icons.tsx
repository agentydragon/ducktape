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

export { default as SyncErrorIcon } from "@tabler/icons-react/dist/esm/icons/IconAlertTriangle.mjs";
export { default as SyncCurrentIcon } from "@tabler/icons-react/dist/esm/icons/IconCircleCheck.mjs";
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
