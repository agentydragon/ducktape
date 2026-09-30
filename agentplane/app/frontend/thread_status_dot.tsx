/** One accessible, colored thread-status dot shared by the thread list and the open-thread composer. */
import { Box, Tooltip } from "@mantine/core";
import type { JSX } from "react";

import "./thread_status_dot.css";

export function ThreadStatusDot({
  color,
  label,
  pulse = false,
  size = "regular",
}: {
  color: string;
  label: string;
  /** Indicates unsettled sync or an active turn. */
  pulse?: boolean;
  size?: "small" | "regular";
}): JSX.Element {
  const className = [
    "agentplane-thread-status-dot",
    size === "small" && "agentplane-thread-status-dot-small",
    pulse && "agentplane-thread-status-dot-pulsing",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <Tooltip label={label} events={{ hover: true, focus: true, touch: true }}>
      <Box
        component="span"
        role="img"
        aria-label={label}
        title={label}
        tabIndex={0}
        className={className}
        style={{ backgroundColor: `var(--mantine-color-${color}-6)` }}
      />
    </Tooltip>
  );
}
