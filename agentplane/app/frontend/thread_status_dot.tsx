/** One accessible, colored thread-status dot shared by the thread list and the open-thread composer. */
import { Box, Tooltip } from "@mantine/core";
import type { JSX } from "react";

import { THREAD_STATUS_BLINK_MS, THREAD_STATUS_COLORS, type ThreadStatusKind } from "./thread_status_palette";
import "./thread_status_dot.css";

export function ThreadStatusDot({
  kind,
  label,
  size = "regular",
}: {
  kind: ThreadStatusKind;
  label: string;
  size?: "small" | "regular";
}): JSX.Element {
  const className = ["agentplane-thread-status-dot", size === "small" && "agentplane-thread-status-dot-small"]
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
        data-status={kind}
        style={{ backgroundColor: THREAD_STATUS_COLORS[kind], animationDuration: `${THREAD_STATUS_BLINK_MS}ms` }}
      />
    </Tooltip>
  );
}
