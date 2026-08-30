import { Badge, Group, Loader, Stack, Text } from "@mantine/core";
import { useEffect, useRef, useState } from "react";

import { makeRecentToolCall, toolApprovalQueueId, type RecentToolCall } from "./approval_state";
import { changedConversationId, useConsoleEvents } from "./console_events";
import { displayableError, fetchPendingApprovals, type ToolCallRecord } from "./client";
import { ApprovalsTab, type ShellChromeProps } from "./shell_chrome";
import { useToolCallDecision } from "./tool_call_decision";

// This is deliberately a tool-call-only surface. Geolocation and screenshot approvals are
// bridge requests owned by the Haku UI iframe; this page has no iframe to receive their result,
// so rendering those buttons here would offer an action with nowhere to deliver its decision.
export function ApprovalsEmbedPage(): JSX.Element {
  const [pendingApprovals, setPendingApprovals] = useState<ToolCallRecord[]>([]);
  const pendingApprovalsRef = useRef<ToolCallRecord[]>([]);
  const [recentToolCalls, setRecentToolCalls] = useState<RecentToolCall[]>([]);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);

  function refresh() {
    setSyncing(true);
    void fetchPendingApprovals()
      .then(
        (approvals) => {
          pendingApprovalsRef.current = approvals;
          setPendingApprovals(approvals);
          setSyncError(null);
        },
        (error: unknown) => setSyncError(displayableError(error))
      )
      .finally(() => setSyncing(false));
  }

  function finishToolDecision(record: ToolCallRecord) {
    const remaining = pendingApprovalsRef.current.filter((approval) => approval.tool_call_id !== record.tool_call_id);
    pendingApprovalsRef.current = remaining;
    setPendingApprovals(remaining);
    const recent = makeRecentToolCall(record, Date.now());
    if (recent) {
      setRecentToolCalls((records) => [
        recent,
        ...records.filter((existing) => existing.record.tool_call_id !== record.tool_call_id),
      ]);
    }
  }

  const toolDecisions = useToolCallDecision({ onSuccess: finishToolDecision, onSettled: refresh });
  const liveStatus = useConsoleEvents((event) => {
    if (changedConversationId(event) === null) refresh();
  });

  useEffect(() => {
    document.title = `Approvals (${pendingApprovals.length}) · Haku`;
  }, [pendingApprovals.length]);

  useEffect(() => {
    if (recentToolCalls.length === 0) return;
    const nextHideAtMs = Math.min(...recentToolCalls.map((recent) => recent.hideAtMs));
    const timer = window.setTimeout(
      () => {
        const now = Date.now();
        setRecentToolCalls((records) => records.filter((recent) => recent.hideAtMs > now));
      },
      Math.max(0, nextHideAtMs - Date.now()) + 50
    );
    return () => window.clearTimeout(timer);
  }, [recentToolCalls]);

  const shellProps = {
    pendingApprovals,
    geolocationApprovals: [],
    screenshotApprovals: [],
    decidingApprovalIds: Array.from(toolDecisions.decidingToolCallIds, toolApprovalQueueId),
    recentToolCalls,
    onApproveTool: (approval: ToolCallRecord, decisionNote?: string) =>
      void toolDecisions.approve(approval, decisionNote),
    onDenyTool: (approval: ToolCallRecord, decisionNote?: string) => void toolDecisions.deny(approval, decisionNote),
    onApproveGeolocation: () => {},
    onDenyGeolocation: () => {},
    onApproveScreenshot: () => {},
    onDenyScreenshot: () => {},
    onDismissRecentToolCall: (toolCallId: string) =>
      setRecentToolCalls((records) => records.filter((recent) => recent.record.tool_call_id !== toolCallId)),
    focusedToolCallId: null,
  } satisfies Pick<
    ShellChromeProps,
    | "pendingApprovals"
    | "geolocationApprovals"
    | "screenshotApprovals"
    | "decidingApprovalIds"
    | "recentToolCalls"
    | "onApproveTool"
    | "onDenyTool"
    | "onApproveGeolocation"
    | "onDenyGeolocation"
    | "onApproveScreenshot"
    | "onDenyScreenshot"
    | "onDismissRecentToolCall"
    | "focusedToolCallId"
  >;

  return (
    <main className="haku-approval-embed" aria-label="Haku approvals">
      <header className="haku-approval-embed-header">
        <Stack gap={2}>
          <Text fw={700}>Haku approvals</Text>
          <Text size="xs" c="dimmed">
            {liveStatus === "live"
              ? "Live updates connected"
              : liveStatus === "offline"
                ? "Live updates unavailable"
                : "Connecting to live updates…"}
          </Text>
        </Stack>
        <Badge color={pendingApprovals.length > 0 ? "yellow" : "gray"} variant="light">
          {pendingApprovals.length}
        </Badge>
      </header>
      <div className="haku-approval-embed-scroll">
        {syncError !== null && (
          <Text c="red" size="sm" mb="sm">
            Failed to load pending approvals: {syncError}
          </Text>
        )}
        {syncing && (
          <Group gap="xs" mb="sm">
            <Loader size="xs" />
            <Text size="xs" c="dimmed">
              Refreshing pending approvals…
            </Text>
          </Group>
        )}
        <div className="haku-approval-embed-list">
          <ApprovalsTab {...shellProps} />
        </div>
      </div>
    </main>
  );
}
