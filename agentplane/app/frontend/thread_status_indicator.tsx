/** One accessible thread-status indicator shared by the thread list and the topbar, at one size. Its look comes from THREAD_STATUS_MARKS. */
import { Box, Tooltip } from "@mantine/core";
import type { JSX } from "react";

import { MarkGlyph } from "./mark_glyph";
import { THREAD_STATUS_MARKS, type ThreadStatusKind } from "./status_mark";
import "./thread_status_indicator.css";

export function ThreadStatusIndicator({ kind, label }: { kind: ThreadStatusKind; label: string }): JSX.Element {
  return (
    <Tooltip label={label} events={{ hover: true, focus: true, touch: true }}>
      <Box
        component="span"
        role="img"
        aria-label={label}
        title={label}
        tabIndex={0}
        className="agentplane-thread-status-indicator"
        data-status={kind}
      >
        <MarkGlyph mark={THREAD_STATUS_MARKS[kind]} size="small" />
      </Box>
    </Tooltip>
  );
}
