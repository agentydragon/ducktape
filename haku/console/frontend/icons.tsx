// Tabler icons via **per-icon subpath imports** — never `import { … } from "@tabler/icons-react"`
// (the barrel OOMs esbuild on RBE at ~8.7 GB; see debug/esbuild_tabler_memory.md). Types for the
// `.mjs` subpaths come from the ambient declaration in `tabler_icons.d.ts`. Thin wrappers keep a
// stable local name + a consistent glyph size; callers can still override via props.
import IconArrowLeft from "@tabler/icons-react/dist/esm/icons/IconArrowLeft.mjs";
import IconHistory from "@tabler/icons-react/dist/esm/icons/IconHistory.mjs";
import IconMapPin from "@tabler/icons-react/dist/esm/icons/IconMapPin.mjs";
import IconMenu2 from "@tabler/icons-react/dist/esm/icons/IconMenu2.mjs";
import IconSettings from "@tabler/icons-react/dist/esm/icons/IconSettings.mjs";
import type { ComponentProps } from "react";

type TablerIconProps = ComponentProps<typeof IconMenu2>;

/** Hamburger — the shell's console-panel toggle. */
export function MenuIcon(props: TablerIconProps) {
  return <IconMenu2 size={20} {...props} />;
}

/** Clock-with-rewind — links to the past-tool-calls history view. */
export function HistoryIcon(props: TablerIconProps) {
  return <IconHistory size={20} {...props} />;
}

/** Left arrow — the full-page views' back-to-embed control. */
export function ArrowLeftIcon(props: TablerIconProps) {
  return <IconArrowLeft size={20} {...props} />;
}

/** Gear — links to the settings view. */
export function SettingsIcon(props: TablerIconProps) {
  return <IconSettings size={20} {...props} />;
}

/** Map pin — the shell's location-sharing control. */
export function MapPinIcon(props: TablerIconProps) {
  return <IconMapPin size={20} {...props} />;
}
