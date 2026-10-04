/** One accessible, colored thread-status indicator shared by the thread list and the open-thread composer: a dot, or moving chevrons for a running turn. */
import { Box, Tooltip } from "@mantine/core";
import type { CSSProperties, JSX } from "react";

import { THREAD_STATUS_COLORS, THREAD_STATUS_RUN_CYCLE_MS, type ThreadStatusKind } from "./thread_status_palette";
import "./thread_status_dot.css";

// Chevrons are drawn in a 14x10 box, one every CHEVRON_PITCH. The track holds enough of them that
// shifting it right by one pitch looks the same as not shifting it, so the CSS animation can loop.
const CHEVRON_PITCH = 5;
const CHEVRON_X = [-7, -2, 3, 8, 13];

function RunningChevrons(): JSX.Element {
  return (
    <svg className="agentplane-thread-status-chevrons" viewBox="0 0 14 10" aria-hidden="true" focusable="false">
      <g
        className="agentplane-thread-status-chevron-track"
        style={
          {
            "--chevron-pitch": `${CHEVRON_PITCH}px`,
            animationDuration: `${THREAD_STATUS_RUN_CYCLE_MS}ms`,
          } as CSSProperties
        }
      >
        {CHEVRON_X.map((x) => (
          <path key={x} d={`M${x} 1.5L${x + 3} 5L${x} 8.5`} />
        ))}
      </g>
    </svg>
  );
}

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
        style={{ color: THREAD_STATUS_COLORS[kind] }}
      >
        {kind === "running" ? <RunningChevrons /> : <span className="agentplane-thread-status-dot-mark" />}
      </Box>
    </Tooltip>
  );
}
