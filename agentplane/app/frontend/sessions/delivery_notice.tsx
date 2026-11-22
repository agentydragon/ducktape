import { Button, Group, Stack, Text } from "@mantine/core";
import { type JSX, useEffect, useMemo, useRef, useState } from "react";

import { deliveryOperationLabel, deliveryStateLabel } from "./delivery_labels";
import {
  DeliveryState,
  type Dispatch,
  type DispatchWorksetItem,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { CommandProgress, waitingReasonLabel, type CommandStage, type CommandSubject } from "./command_progress";
import { noticeKey, type NoticeAttention } from "./command_notice_attention";
import { readFullDispatchReason, type ReadDispatchReceipts, type ReasonLookup } from "./dispatch_workset";

import type { ThreadDeliveryState } from "./thread_delivery";
import { MAX_DISPATCH_BATCH } from "./client";

export function AcknowledgeDeliveryIssue({
  item,
  attention,
}: {
  item: DispatchWorksetItem;
  attention: NoticeAttention;
}): JSX.Element | null {
  if (!attention.canAcknowledge(item)) return null;
  const saving = attention.pending === noticeKey(item);
  return (
    <Button
      size="compact-xs"
      variant="subtle"
      loading={saving}
      disabled={attention.pending !== null && !saving}
      onClick={() => attention.acknowledge(item)}
      aria-label={`Dismiss delivery issue for command ${item.commandId}`}
    >
      Dismiss
    </Button>
  );
}

export function ReceiptDetails({
  readReceipts,
  item,
}: {
  readReceipts: ReadDispatchReceipts;
  item: DispatchWorksetItem;
}): JSX.Element {
  const { reason, actions } = useReceiptReason(readReceipts, item);
  return (
    <Stack gap={0} data-dispatch-command-id={item.commandId} style={{ overflowWrap: "anywhere" }}>
      <Text size="sm">
        {deliveryOperationLabel(item.operation)} · {deliveryStateLabel[item.state]}
      </Text>
      {item.state === DeliveryState.UNKNOWN && item.journalAdmitted && (
        <Text size="xs" c="dimmed">
          Runner accepted the command
        </Text>
      )}
      {reason && (
        <Text size="xs" c="dimmed">
          {waitingReasonLabel(reason)}
        </Text>
      )}
      {actions && <Group gap="xs">{actions}</Group>}
    </Stack>
  );
}

function useReceiptReason(readReceipts: ReadDispatchReceipts, item: DispatchWorksetItem) {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<ReasonLookup | null>(null);
  const stopLookup = useRef<(() => void) | null>(null);
  useEffect(() => () => stopLookup.current?.(), []);
  const showReason = () => {
    if (stopLookup.current) return;
    setResult(null);
    setLoading(true);
    stopLookup.current = readFullDispatchReason(readReceipts, item, (next) => {
      stopLookup.current = null;
      setResult(next);
      setLoading(false);
    });
  };
  const fullReason = result && "reason" in result ? result.reason : undefined;
  const message = fullReason ?? item.reason;
  const reason = message === "sandbox_deleted" ? "Sandbox was deleted" : message;
  const showFullReason = item.reasonTruncated && fullReason === undefined;

  return {
    reason: showFullReason ? `${reason}…` : reason,
    actions: showFullReason ? (
      <>
        <Button size="compact-xs" variant="subtle" loading={loading} onClick={showReason}>
          Show full reason
        </Button>
        {result && "error" in result && (
          <Text size="xs" role="status">
            {result.error}
          </Text>
        )}
      </>
    ) : null,
  };
}

const receiptStage: Partial<Record<DeliveryState, CommandStage>> = {
  [DeliveryState.QUEUED_IN_DISPATCHER]: "queued",
  [DeliveryState.DELIVERING_TO_RUNNER]: "delivering",
  [DeliveryState.RUNNER_ADMITTED]: "admitted",
  [DeliveryState.FAILED]: "dispatch_failed",
  [DeliveryState.CANCELLED_IN_DISPATCHER]: "cancelled",
  [DeliveryState.UNKNOWN]: "unknown",
};

export function DispatchReceiptProgress({
  delivery,
  item,
  subject,
}: {
  delivery: ThreadDeliveryState;
  item: DispatchWorksetItem;
  subject: CommandSubject;
}): JSX.Element {
  const { reason, actions } = useReceiptReason(delivery.readReceipts, item);
  const accepted = item.state === DeliveryState.UNKNOWN && item.journalAdmitted && (
    <Text size="xs" c="dimmed">
      Runner accepted the command
    </Text>
  );
  const stage = receiptStage[item.state] ?? "unknown";
  return (
    <Group gap={4} wrap="nowrap" className="agentplane-command-delivery">
      <CommandProgress
        stage={stage}
        subject={subject}
        reason={reason}
        details={
          accepted || actions ? (
            <>
              {accepted}
              {actions}
            </>
          ) : undefined
        }
      />
      <AcknowledgeDeliveryIssue item={item} attention={delivery.attention} />
    </Group>
  );
}
export function useDispatchOriginals(
  readReceipts: ReadDispatchReceipts,
  ids: readonly string[]
): { commands: ReadonlyMap<string, Dispatch> | undefined; complete: boolean } {
  const idsKey = JSON.stringify([...new Set(ids)].sort());
  const read = useMemo(() => ({ ids: JSON.parse(idsKey) as string[], readReceipts }), [idsKey, readReceipts]);
  const [result, setResult] = useState<{
    read: typeof read;
    commands: ReadonlyMap<string, Dispatch>;
  } | null>(null);
  useEffect(() => {
    if (!read.ids.length) return;
    const controller = new AbortController();
    const batches = [];
    for (let start = 0; start < read.ids.length; start += MAX_DISPATCH_BATCH) {
      batches.push(read.readReceipts(read.ids.slice(start, start + MAX_DISPATCH_BATCH), controller.signal));
    }
    void Promise.allSettled(batches).then((results) => {
      if (controller.signal.aborted) return;
      const commands = new Map<string, Dispatch>();
      for (const batch of results) {
        if (batch.status !== "fulfilled") continue;
        for (const { result } of batch.value) {
          if (result.case === "dispatch" && result.value.command?.commandId === result.value.commandId) {
            commands.set(result.value.commandId, result.value);
          }
        }
      }
      setResult({ read, commands });
    });
    return () => controller.abort();
  }, [read]);
  return {
    commands: result?.read.readReceipts === readReceipts ? result.commands : undefined,
    complete: result?.read === read,
  };
}
