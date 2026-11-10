import { ActionIcon } from "@mantine/core";
import IconZoomCode from "@tabler/icons-react/dist/esm/icons/IconZoomCode.mjs";
import { type CSSProperties, type JSX } from "react";

import "./inspection.css";

/** A small control for revealing details owned by a transcript item. */
export function InspectionToggle({
  open,
  onChange,
  label,
  placement = "overlay",
  controls,
  style,
}: {
  open: boolean;
  onChange: (open: boolean) => void;
  label: string;
  placement?: "overlay" | "inline" | "bubble";
  controls?: string;
  style?: CSSProperties;
}): JSX.Element {
  return (
    <ActionIcon
      size="xs"
      variant={open ? "light" : "subtle"}
      color="gray"
      className="transcript-inspection-toggle"
      data-placement={placement}
      aria-label={label}
      title={label}
      aria-expanded={open}
      aria-controls={controls}
      onClick={() => onChange(!open)}
      style={style}
    >
      <IconZoomCode size={14} />
    </ActionIcon>
  );
}
