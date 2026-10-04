/** One accessible thread-status indicator shared by the thread list and the open-thread composer. Its look comes from THREAD_STATUS_MARKS. */
import { Box, Tooltip } from "@mantine/core";
import type { JSX } from "react";

import { MarkGlyph } from "./mark_glyph";
import { THREAD_STATUS_MARKS, type ThreadStatusKind } from "./status_mark";
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
  return (
    <Tooltip label={label} events={{ hover: true, focus: true, touch: true }}>
      <Box
        component="span"
        role="img"
        aria-label={label}
        title={label}
        tabIndex={0}
        className="agentplane-thread-status-dot"
        data-status={kind}
      >
        <MarkGlyph mark={THREAD_STATUS_MARKS[kind]} size={size} />
      </Box>
    </Tooltip>
  );
}
