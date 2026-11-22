import { Button, Divider, Group, Modal, ScrollArea, Stack, Tabs, Text } from "@mantine/core";
import { type JSX, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import type { CommandNoticeAcknowledgement, CommandNoticeRevision } from "../../app_service_pb";
import type {
  Dispatch,
  DispatchWorksetItem,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { displayableError } from "../transport/client";
import { TopbarActions } from "../shell/topbar";
import { TimeAgo } from "../ui/time_ago";
import { listSessionDispatches } from "./client";
import { isDeliveryIssue, noticeKey } from "./command_notice_attention";
import { listCommandNoticeAcknowledgements } from "./command_notice_client";
import { storedCommandLabel, deliveryStateLabel } from "./delivery_labels";
import { readSelectedDispatchReceipts, worksetItemFromReceipt, type ReadDispatchReceipts } from "./dispatch_workset";
import { AcknowledgeDeliveryIssue, ReceiptDetails, useDispatchOriginals } from "./delivery_notice";
import { StoredCommandDetails } from "./stored_command_details";
import type { ThreadDeliveryState } from "./thread_delivery";
import { timestampLabel } from "./thread_presentation";

interface Page<T> {
  rows: T[];
  nextPageToken?: string;
}

export function PagedHistory<T>({
  sessionId,
  load,
  renderRows,
  revision = 0,
}: {
  sessionId: string;
  load: (sessionId: string, token: string, signal: AbortSignal) => Promise<Page<T>>;
  renderRows: (rows: T[], more: boolean) => ReactNode;
  revision?: number;
}): JSX.Element {
  const [tokens, setTokens] = useState([""]);
  const [retry, setRetry] = useState(0);
  const token = tokens[tokens.length - 1];
  const [result, setResult] = useState<{
    token: string;
    retry: number;
    revision: number;
    page?: Page<T>;
    error?: string;
  } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    load(sessionId, token, controller.signal).then(
      (page) => {
        if (!controller.signal.aborted) setResult({ token, retry, revision, page });
      },
      (error: unknown) => {
        if (!controller.signal.aborted) setResult({ token, retry, revision, error: displayableError(error) });
      }
    );
    return () => controller.abort();
  }, [load, retry, revision, sessionId, token]);
  const loaded = result?.token === token && result.retry === retry && result.revision === revision ? result : null;
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

function RetainedReceiptRows({ delivery, rows }: { delivery: ThreadDeliveryState; rows: Dispatch[] }) {
  const { attention, readReceipts, registerArchiveIssues } = delivery;
  const originals = useDispatchOriginals(
    readReceipts,
    rows.map((row) => row.commandId)
  );
  const notices = useMemo(() => rows.filter((row) => isDeliveryIssue(row.state)).map(worksetItemFromReceipt), [rows]);
  useEffect(() => registerArchiveIssues(notices), [notices, registerArchiveIssues]);
  const byCommand = new Map(notices.map((notice) => [notice.commandId, notice]));
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
        const notice = byCommand.get(row.commandId);
        return (
          <Stack key={row.commandId} gap={2} data-receipt-id={row.commandId} style={{ overflowWrap: "anywhere" }}>
            <Divider />
            <Text size="sm" fw={500}>
              {storedCommandLabel(row.operation, originals.commands?.get(row.commandId)?.command)}
              {" · "}
              {deliveryStateLabel[row.state] ?? "Delivery unconfirmed"}
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

function RetainedDispatches({ delivery }: { delivery: ThreadDeliveryState }): JSX.Element {
  return (
    <Stack gap="sm">
      <Text size="xs" c="dimmed">
        Showing retained commands, newest first. Older deletion receipts are outside this history.
      </Text>
      <PagedHistory
        sessionId={delivery.sessionId}
        load={retained}
        renderRows={(rows) => <RetainedReceiptRows delivery={delivery} rows={rows} />}
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

function AcknowledgedReceiptEvidence({
  readReceipts,
  notice,
}: {
  readReceipts: ReadDispatchReceipts;
  notice: CommandNoticeRevision;
}) {
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
    void readSelectedDispatchReceipts(readReceipts, [notice.commandId], controller.signal)
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
  if (result && "item" in result) return <ReceiptDetails readReceipts={readReceipts} item={result.item} />;
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

function AcknowledgedReceiptRows({
  delivery,
  rows,
}: {
  delivery: ThreadDeliveryState;
  rows: CommandNoticeAcknowledgement[];
}): JSX.Element {
  const { readReceipts, attention } = delivery;
  const current = new Map(attention.issues.map((item) => [noticeKey(item), item]));
  const originals = useDispatchOriginals(
    readReceipts,
    rows.flatMap(({ notice }) => (notice ? [notice.commandId] : []))
  );
  if (rows.length === 0)
    return (
      <Text size="sm" c="dimmed">
        No acknowledged delivery issues.
      </Text>
    );
  return (
    <>
      {rows.map(({ notice, acknowledgedAt }) => {
        if (!notice) return null;
        const item = current.get(noticeKey(notice));
        const original = originals.commands?.get(notice.commandId);
        const stamp = timestampLabel(acknowledgedAt);
        return (
          <Stack key={noticeKey(notice)} gap={2} style={{ overflowWrap: "anywhere" }}>
            <Divider />
            <Group gap="xs" justify="space-between" align="baseline">
              <Text size={original ? "sm" : "xs"} ff={original ? undefined : "monospace"} title={notice.commandId}>
                {original ? storedCommandLabel(original.operation, original.command) : notice.commandId}
              </Text>
              {stamp && (
                <Text size="xs" c="dimmed">
                  <TimeAgo value={stamp} />
                </Text>
              )}
            </Group>
            {item ? (
              <ReceiptDetails readReceipts={readReceipts} item={item} />
            ) : (
              <AcknowledgedReceiptEvidence readReceipts={readReceipts} notice={notice} />
            )}
          </Stack>
        );
      })}
    </>
  );
}

function AcknowledgedIssues({ delivery }: { delivery: ThreadDeliveryState }) {
  return (
    <PagedHistory
      sessionId={delivery.sessionId}
      load={acknowledged}
      revision={delivery.attention.historyRevision}
      renderRows={(rows) => <AcknowledgedReceiptRows delivery={delivery} rows={rows} />}
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
  delivery,
  opened,
  onClose,
}: {
  delivery: ThreadDeliveryState;
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
            <RetainedDispatches delivery={delivery} />
          </Tabs.Panel>
          <Tabs.Panel value="acknowledged">
            <AcknowledgedIssues delivery={delivery} />
          </Tabs.Panel>
        </Tabs>
      )}
    </Modal>
  );
}
