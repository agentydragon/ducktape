import { Button, Group, Paper, ScrollArea, Stack, Text } from "@mantine/core";
import { type JSX, type ReactNode, useEffect, useRef, useState } from "react";

import { ThreadCommandOutcome, type ThreadEntity } from "../../../history_service/api/history/history_service_pb";
import { deliveryOperationLabel, deliveryStateLabel } from "./delivery_labels";
import {
  DeliveryState,
  type DispatchWorksetItem,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { waitingReasonLabel } from "./command_progress";
import {
  isDeliveryIssue,
  NO_ISSUES,
  noticeKey,
  useCommandNoticeAttention,
  type NoticeAttention,
} from "./command_notice_attention";
import {
  EMPTY_WORKSET,
  readFullDispatchReason,
  watchDispatchWorkset,
  type ReasonLookup,
  type WorksetView,
} from "./dispatch_workset";
import type { LocalCommand } from "./local_commands";

export function AcknowledgeDeliveryIssue({
  item,
  attention,
}: {
  item: DispatchWorksetItem;
  attention: NoticeAttention;
}): JSX.Element | null {
  if (attention.stale || !attention.issues.includes(item) || (item.receiptVersion ?? 0n) <= 0n) return null;
  const saving = attention.pending === noticeKey(item);
  return (
    <Button
      size="compact-xs"
      variant="subtle"
      loading={saving}
      disabled={attention.pending !== null && !saving}
      onClick={() => attention.acknowledge(item)}
      aria-label={`Acknowledge delivery issue for command ${item.commandId}`}
    >
      Acknowledge
    </Button>
  );
}

export function WorksetRow({
  sessionId,
  item,
  action,
  showCommandId = true,
}: {
  sessionId: string;
  item: DispatchWorksetItem;
  action?: ReactNode;
  showCommandId?: boolean;
}): JSX.Element {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<ReasonLookup | null>(null);
  const stopLookup = useRef<(() => void) | null>(null);
  useEffect(() => () => stopLookup.current?.(), []);
  const showReason = () => {
    if (stopLookup.current) return;
    setResult(null);
    setLoading(true);
    stopLookup.current = readFullDispatchReason(sessionId, item, (next) => {
      stopLookup.current = null;
      setResult(next);
      setLoading(false);
    });
  };
  const fullReason = result && "reason" in result ? result.reason : undefined;
  const reason = fullReason ?? item.reason;
  const showFullReason = item.reasonTruncated && fullReason === undefined;
  return (
    <Stack gap={0} data-dispatch-command-id={item.commandId} style={{ overflowWrap: "anywhere" }}>
      <Group gap="xs" justify="space-between" align="baseline">
        <Text size="sm">
          {deliveryOperationLabel(item.operation)} · {deliveryStateLabel[item.state]}
        </Text>
        {showCommandId && (
          <Text size="xs" c="dimmed" ff="monospace" title={item.commandId} aria-label={`Command ${item.commandId}`}>
            {item.commandId.slice(0, 8)}
          </Text>
        )}
      </Group>
      {item.state === DeliveryState.UNKNOWN && item.journalAdmitted && (
        <Text size="xs" c="dimmed">
          Runner accepted the command
        </Text>
      )}
      {reason && (
        <Text size="xs" c="dimmed">
          {reason === "sandbox_deleted" ? "Sandbox was deleted" : waitingReasonLabel(reason)}
          {showFullReason && "…"}
        </Text>
      )}
      {(showFullReason || action) && (
        <Group gap="xs">
          {showFullReason && (
            <Button size="compact-xs" variant="subtle" loading={loading} onClick={showReason}>
              Show full reason
            </Button>
          )}
          {action}
          {result && "error" in result && (
            <Text size="xs" c="dimmed" role="status">
              {result.error}
            </Text>
          )}
        </Group>
      )}
    </Stack>
  );
}

export function DispatchWorksetPanel({
  sessionId,
  view,
  onOpenDeliveryHistory,
  localCommands = [],
  entities = [],
}: {
  sessionId: string;
  view: WorksetView;
  onOpenDeliveryHistory: () => void;
  localCommands?: readonly LocalCommand[];
  entities?: readonly ThreadEntity[];
}): JSX.Element | null {
  const { snapshot, stale, coverageLost } = view;
  const attention = useCommandNoticeAttention(sessionId, snapshot?.recentIssues ?? NO_ISSUES, stale);
  const rows = snapshot ? [...snapshot.active, ...attention.issues, ...snapshot.recentAdmittedOrCancelled] : [];
  const hidden = new Set(localCommands.map((command) => command.command.commandId));
  const pendingInHistory = new Set<string>();
  for (const row of entities) {
    if (row.entity.case === "confirmedInput") {
      for (const id of row.entity.value.originCommandIds) hidden.add(id);
    } else if (row.entity.case === "command") {
      const outcome = row.entity.value.outcome;
      if (outcome === ThreadCommandOutcome.PENDING || outcome === ThreadCommandOutcome.UNSPECIFIED) {
        pendingInHistory.add(row.entityId);
      } else {
        hidden.add(row.entityId);
      }
    }
  }
  const visible = rows.filter(
    (item) =>
      !hidden.has(item.commandId) &&
      !attention.acknowledged(item) &&
      (!pendingInHistory.has(item.commandId) || isDeliveryIssue(item.state))
  );
  const hasOlderIssues = snapshot?.moreRecentIssuesAvailable;
  const limits = [
    snapshot?.moreActiveAvailable && "More active commands are outside this view.",
    snapshot?.moreRecentAdmittedOrCancelledAvailable && "Older admissions or cancellations are outside this view.",
  ].filter(Boolean);
  if (!visible.length && !stale && !coverageLost && !hasOlderIssues && !limits.length && !attention.error) return null;
  return (
    <Paper component="section" aria-label="Dispatcher delivery" p="xs" withBorder>
      <Stack gap="xs">
        <Text size="sm" fw={600}>
          Dispatcher delivery
        </Text>
        {stale && (
          <Text size="xs" c="dimmed" role="status">
            {snapshot
              ? "Showing the last delivery update while reconnecting."
              : "Delivery updates are unavailable while reconnecting."}
          </Text>
        )}
        {coverageLost && (
          <Text size="xs" c="dimmed" role="status">
            Some earlier delivery receipts may have expired.
          </Text>
        )}
        {attention.error && (
          <Text size="xs" role="alert">
            {attention.error}
          </Text>
        )}
        {hasOlderIssues && (
          <Button
            size="compact-xs"
            variant="subtle"
            style={{ alignSelf: "flex-start" }}
            onClick={onOpenDeliveryHistory}
          >
            Older delivery issues
          </Button>
        )}
        {limits.length > 0 && (
          <Text size="xs" c="dimmed">
            {limits.join(" ")}
          </Text>
        )}
        {visible.length > 0 && (
          <ScrollArea.Autosize mah={260} type="auto">
            <Stack gap="xs">
              {visible.map((item) => (
                <WorksetRow
                  key={`${noticeKey(item)}:${item.state}:${item.updatedAt?.seconds}:${item.updatedAt?.nanos}`}
                  sessionId={sessionId}
                  item={item}
                  action={<AcknowledgeDeliveryIssue item={item} attention={attention} />}
                />
              ))}
            </Stack>
          </ScrollArea.Autosize>
        )}
      </Stack>
    </Paper>
  );
}

export function useDispatchWorkset(sessionId: string | null): WorksetView {
  const [state, setState] = useState<{ sessionId: string; view: WorksetView } | null>(null);
  useEffect(() => {
    if (sessionId !== null) return watchDispatchWorkset(sessionId, (view) => setState({ sessionId, view }));
  }, [sessionId]);
  return state?.sessionId === sessionId ? state.view : EMPTY_WORKSET;
}
