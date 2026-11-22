import { Button, Group, Paper, ScrollArea, Stack, Text } from "@mantine/core";
import { type JSX, useEffect, useRef, useState } from "react";

import { ThreadCommandOutcome, type ThreadEntity } from "../../../history_service/api/history/history_service_pb";
import { Operation } from "../../../proto/command_pb";
import {
  DeliveryState,
  type DispatchWorksetItem,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { waitingReasonLabel } from "./command_progress";
import { EMPTY_WORKSET, readFullDispatchReason, watchDispatchWorkset, type ReasonLookup } from "./dispatch_workset";
import type { LocalCommand } from "./local_commands";

function operationLabel(operation: Operation): string {
  switch (operation) {
    case Operation.SUBMIT_INPUT:
      return "Send message";
    case Operation.CHANGE_MODEL:
      return "Model change";
    case Operation.CHANGE_REASONING_EFFORT:
      return "Reasoning effort change";
    case Operation.INTERRUPT_TURN:
      return "Turn interrupt";
    default:
      return "Command";
  }
}

const stateLabel: Record<DeliveryState, string> = {
  [DeliveryState.UNSPECIFIED]: "Delivery unconfirmed",
  [DeliveryState.QUEUED_IN_DISPATCHER]: "Queued in Dispatcher",
  [DeliveryState.DELIVERING_TO_RUNNER]: "Sending to runner",
  [DeliveryState.RUNNER_ADMITTED]: "Runner accepted",
  [DeliveryState.FAILED]: "Delivery failed",
  [DeliveryState.CANCELLED_IN_DISPATCHER]: "Cancelled before delivery",
  [DeliveryState.UNKNOWN]: "Outcome unknown",
};

function WorksetRow({ sessionId, item }: { sessionId: string; item: DispatchWorksetItem }): JSX.Element {
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
          {operationLabel(item.operation)} · {stateLabel[item.state]}
        </Text>
        <Text size="xs" c="dimmed" ff="monospace" title={item.commandId} aria-label={`Command ${item.commandId}`}>
          {item.commandId.slice(0, 8)}
        </Text>
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
      {showFullReason && (
        <Group gap="xs">
          <Button size="compact-xs" variant="subtle" loading={loading} onClick={showReason}>
            Show full reason
          </Button>
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
  localCommands,
  entities,
}: {
  sessionId: string;
  localCommands: readonly LocalCommand[];
  entities: readonly ThreadEntity[];
}): JSX.Element | null {
  const [view, setView] = useState(EMPTY_WORKSET);
  useEffect(() => watchDispatchWorkset(sessionId, setView), [sessionId]);
  const { snapshot, stale, coverageLost } = view;
  const rows = snapshot ? [...snapshot.active, ...snapshot.recentIssues, ...snapshot.recentAdmittedOrCancelled] : [];
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
      (!pendingInHistory.has(item.commandId) ||
        item.state === DeliveryState.UNKNOWN ||
        item.state === DeliveryState.FAILED)
  );
  const limits = [
    snapshot?.moreActiveAvailable && "More active commands are outside this view.",
    snapshot?.moreRecentIssuesAvailable && "Older delivery issues are outside this view.",
    snapshot?.moreRecentAdmittedOrCancelledAvailable && "Older admissions or cancellations are outside this view.",
  ].filter(Boolean);
  if (!visible.length && !stale && !coverageLost && !limits.length) return null;
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
                  key={`${item.commandId}:${item.state}:${item.updatedAt?.seconds}:${item.updatedAt?.nanos}`}
                  sessionId={sessionId}
                  item={item}
                />
              ))}
            </Stack>
          </ScrollArea.Autosize>
        )}
      </Stack>
    </Paper>
  );
}
