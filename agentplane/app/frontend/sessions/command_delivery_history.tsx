import { Button, Divider, Group, Modal, ScrollArea, Stack, Tabs, Text } from "@mantine/core";
import { type JSX, type ReactNode, useEffect, useRef, useState } from "react";
import type { CommandNoticeAcknowledgement, CommandNoticeRevision } from "../../app_service_pb";
import type {
  Dispatch,
  DispatchWorksetItem,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { displayableError } from "../transport/client";
import { TopbarActions } from "../shell/topbar";
import { TimeAgo } from "../ui/time_ago";
import { listSessionDispatches } from "./client";
import { isDeliveryIssue, noticeKey, useCommandNoticeAttention } from "./command_notice_attention";
import { listCommandNoticeAcknowledgements } from "./command_notice_client";
import { deliveryOperationLabel, deliveryStateLabel } from "./delivery_labels";
import { readSelectedDispatchReceipts, worksetItemFromReceipt } from "./dispatch_workset";
import { AcknowledgeDeliveryIssue, WorksetRow } from "./dispatch_workset_panel";
import { StoredCommandDetails } from "./stored_command_details";
import { timestampLabel } from "./thread_presentation";

interface Page<T> {
  rows: T[];
  nextPageToken?: string;
}

export function PagedHistory<T>({
  sessionId,
  load,
  renderRows,
}: {
  sessionId: string;
  load: (sessionId: string, token: string, signal: AbortSignal) => Promise<Page<T>>;
  renderRows: (rows: T[], more: boolean) => ReactNode;
}): JSX.Element {
  const [tokens, setTokens] = useState([""]);
  const [retry, setRetry] = useState(0);
  const token = tokens[tokens.length - 1];
  const [result, setResult] = useState<{ token: string; retry: number; page?: Page<T>; error?: string } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    load(sessionId, token, controller.signal).then(
      (page) => {
        if (!controller.signal.aborted) setResult({ token, retry, page });
      },
      (error: unknown) => {
        if (!controller.signal.aborted) setResult({ token, retry, error: displayableError(error) });
      }
    );
    return () => controller.abort();
  }, [load, retry, sessionId, token]);
  const loaded = result?.token === token && result.retry === retry ? result : null;
  const next = loaded?.page?.nextPageToken;
  return (
    <Stack gap="sm">
      {!loaded && (
        <Text role="status" size="sm">
          Loading delivery history…
        </Text>
      )}
      {loaded?.error && (
        <Stack gap="xs" align="flex-start">
          <Text role="alert" size="sm">
            Could not load this page. {loaded.error}
          </Text>
          <Button variant="subtle" size="compact-sm" onClick={() => setRetry(retry + 1)}>
            Retry
          </Button>
        </Stack>
      )}
      {loaded?.page && renderRows(loaded.page.rows, Boolean(next))}
      {(tokens.length > 1 || next) && (
        <Group justify="space-between" gap="xs">
          <Button
            size="compact-sm"
            variant="subtle"
            disabled={tokens.length === 1}
            onClick={() => setTokens(tokens.slice(0, -1))}
          >
            Newer
          </Button>
          <Text size="xs" c="dimmed">
            Page {tokens.length}
          </Text>
          <Button
            size="compact-sm"
            variant="subtle"
            disabled={!next}
            onClick={() => next && setTokens([...tokens, next])}
          >
            Older
          </Button>
        </Group>
      )}
      {loaded?.error && tokens.length > 1 && (
        <Button size="compact-sm" variant="subtle" onClick={() => setTokens([""])}>
          Back to newest
        </Button>
      )}
    </Stack>
  );
}

async function retained(sessionId: string, pageToken: string, signal: AbortSignal): Promise<Page<Dispatch>> {
  const { dispatches, nextPageToken } = await listSessionDispatches(sessionId, { pageToken, signal });
  return { rows: dispatches, nextPageToken };
}

function RetainedReceiptRows({ sessionId, rows }: { sessionId: string; rows: Dispatch[] }) {
  const notices = new Map(
    rows.filter((row) => isDeliveryIssue(row.state)).map((row) => [row.commandId, worksetItemFromReceipt(row)])
  );
  const attention = useCommandNoticeAttention(sessionId, [...notices.values()], false);
  if (rows.length === 0)
    return (
      <Text size="sm" c="dimmed">
        No retained receipts.
      </Text>
    );
  return (
    <>
      {attention.error && (
        <Text size="xs" role="alert">
          {attention.error}
        </Text>
      )}
      {rows.map((row) => {
        const time = timestampLabel(row.updatedAt);
        const notice = notices.get(row.commandId);
        return (
          <Stack key={row.commandId} gap={2} style={{ overflowWrap: "anywhere" }}>
            <Divider />
            <Text size="sm" fw={500}>
              {deliveryOperationLabel(row.operation)} · {deliveryStateLabel[row.state] ?? "Delivery unconfirmed"}
            </Text>
            <Group gap="xs" justify="space-between" align="baseline">
              <Text size="xs" ff="monospace" c="dimmed">
                {row.commandId}
              </Text>
              {time && (
                <Text size="xs" c="dimmed">
                  <TimeAgo value={time} />
                </Text>
              )}
            </Group>
            {row.reason && (
              <Text size="xs" style={{ whiteSpace: "pre-wrap" }}>
                {row.reason}
              </Text>
            )}
            {notice &&
              (attention.acknowledged(notice) ? (
                <Text size="xs" c="dimmed">
                  Acknowledged
                </Text>
              ) : (
                <Group>
                  <AcknowledgeDeliveryIssue item={notice} attention={attention} />
                </Group>
              ))}
            <StoredCommandDetails receipt={row} />
          </Stack>
        );
      })}
    </>
  );
}

export function RetainedDispatches({ sessionId }: { sessionId: string }): JSX.Element {
  return (
    <Stack gap="sm">
      <Text size="xs" c="dimmed">
        Showing retained commands, newest first. Older deletion receipts are outside this history.
      </Text>
      <PagedHistory
        key={sessionId}
        sessionId={sessionId}
        load={retained}
        renderRows={(rows) => <RetainedReceiptRows sessionId={sessionId} rows={rows} />}
      />
    </Stack>
  );
}

async function acknowledged(
  sessionId: string,
  pageToken: string,
  signal: AbortSignal
): Promise<Page<CommandNoticeAcknowledgement>> {
  const { acknowledgements, nextPageToken } = await listCommandNoticeAcknowledgements(sessionId, pageToken, signal);
  return { rows: acknowledgements, nextPageToken };
}

function AcknowledgedReceiptEvidence({ sessionId, notice }: { sessionId: string; notice: CommandNoticeRevision }) {
  const controllerRef = useRef<AbortController | null>(null);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{ item: DispatchWorksetItem } | { unavailable: string; retry: boolean } | null>(
    null
  );
  useEffect(() => () => controllerRef.current?.abort(), []);
  const inspect = () => {
    const controller = new AbortController();
    controllerRef.current?.abort();
    controllerRef.current = controller;
    setLoading(true);
    void readSelectedDispatchReceipts(sessionId, [notice.commandId], controller.signal)
      .then(([receipt]) => {
        if (controller.signal.aborted) return;
        setResult(
          receipt?.receiptVersion === notice.receiptVersion && isDeliveryIssue(receipt.state)
            ? { item: worksetItemFromReceipt(receipt) }
            : {
                unavailable: receipt
                  ? "The original receipt revision is unavailable. Dispatcher has a different revision."
                  : "Dispatcher could not return the original receipt revision.",
                retry: false,
              }
        );
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted)
          setResult({ unavailable: `Could not inspect the receipt. ${displayableError(error)}`, retry: true });
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
  };
  if (result && "item" in result) return <WorksetRow sessionId={sessionId} item={result.item} showCommandId={false} />;
  return (
    <Group gap="xs">
      {(!result || result.retry) && (
        <Button size="compact-xs" variant="subtle" loading={loading} onClick={inspect}>
          Inspect original receipt
        </Button>
      )}
      {result && (
        <Text size="xs" c="dimmed" role="status">
          {result.unavailable}
        </Text>
      )}
    </Group>
  );
}

function AcknowledgedIssues({ sessionId, issues }: { sessionId: string; issues: readonly DispatchWorksetItem[] }) {
  const current = new Map(issues.map((item) => [noticeKey(item), item]));
  return (
    <PagedHistory
      sessionId={sessionId}
      load={acknowledged}
      renderRows={(rows) =>
        rows.length === 0 ? (
          <Text size="sm" c="dimmed">
            No acknowledged delivery issues.
          </Text>
        ) : (
          rows.map(({ notice, acknowledgedAt }) => {
            if (!notice) return null;
            const item = current.get(noticeKey(notice));
            const stamp = timestampLabel(acknowledgedAt);
            return (
              <Stack key={noticeKey(notice)} gap={2} style={{ overflowWrap: "anywhere" }}>
                <Divider />
                <Group gap="xs" justify="space-between" align="baseline">
                  <Text size="xs" ff="monospace" c="dimmed">
                    {notice.commandId}
                  </Text>
                  {stamp && (
                    <Text size="xs" c="dimmed">
                      <TimeAgo value={stamp} />
                    </Text>
                  )}
                </Group>
                {item ? (
                  <WorksetRow sessionId={sessionId} item={item} showCommandId={false} />
                ) : (
                  <AcknowledgedReceiptEvidence sessionId={sessionId} notice={notice} />
                )}
              </Stack>
            );
          })
        )
      }
    />
  );
}

/** There is no conversation overflow menu until its first History window arrives. */
export function DeliveryHistoryFallback({ onOpen }: { onOpen: () => void }): JSX.Element {
  return (
    <TopbarActions>
      <Button size="compact-sm" variant="subtle" onClick={onOpen}>
        Delivery history
      </Button>
    </TopbarActions>
  );
}

export function CommandDeliveryHistory({
  sessionId,
  issues,
  opened,
  onClose,
}: {
  sessionId: string;
  issues: readonly DispatchWorksetItem[];
  opened: boolean;
  onClose: () => void;
}): JSX.Element {
  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title="Delivery history"
      size="lg"
      scrollAreaComponent={ScrollArea.Autosize}
    >
      {opened && (
        <Tabs defaultValue="receipts" keepMounted={false}>
          <Tabs.List mb="sm">
            <Tabs.Tab value="receipts">Retained receipts</Tabs.Tab>
            <Tabs.Tab value="acknowledged">Acknowledged</Tabs.Tab>
          </Tabs.List>
          <Tabs.Panel value="receipts">
            <RetainedDispatches sessionId={sessionId} />
          </Tabs.Panel>
          <Tabs.Panel value="acknowledged">
            <AcknowledgedIssues sessionId={sessionId} issues={issues} />
          </Tabs.Panel>
        </Tabs>
      )}
    </Modal>
  );
}
