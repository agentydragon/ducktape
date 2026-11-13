/** One accessible thread-status indicator shared by thread rows and the topbar. */
import { Box, Tooltip } from "@mantine/core";
import type { JSX } from "react";

import { MarkGlyph } from "../mark_glyph";
import { THREAD_STATUS_MARKS, type ThreadStatusKind } from "../status_mark";
import { ThreadNoticeBadge } from "./thread_notices";
import type { Inbox } from "../../../notification_service/api/notifications/notifications_pb";
import "./thread_status_indicator.css";

export function ThreadStatusIndicator({
  kind,
  label,
  inbox,
  live = false,
}: {
  kind: ThreadStatusKind;
  label: string;
  inbox?: Inbox;
  live?: boolean;
}): JSX.Element {
  return (
    <span className="agentplane-thread-status-indicator" data-status={kind} aria-label={label}>
      <Tooltip label={label} events={{ hover: true, focus: true, touch: true }}>
        <Box
          component="span"
          role="img"
          aria-label={label}
          title={label}
          tabIndex={0}
          className="agentplane-thread-status-mark"
        >
          <MarkGlyph mark={THREAD_STATUS_MARKS[kind]} size="small" />
        </Box>
      </Tooltip>
      <ThreadNoticeBadge inbox={inbox} live={live} />
    </span>
  );
}
