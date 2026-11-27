import { Button, Divider, Group, Modal, ScrollArea, Stack, Tabs, Text } from "@mantine/core";
import { type JSX, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { create } from "@bufbuild/protobuf";
import type { CommandNoticeAcknowledgement, CommandNoticeRevision } from "../../app_service_pb";
import {
  ReadSessionEventsRequestSchema,
  ThreadCommandOutcome,
  type ThreadEntity,
} from "../../../history_service/api/history/history_service_pb";
import type {
  Dispatch,
  DispatchWorksetItem,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { displayableError } from "../transport/client";
import { TopbarActions } from "../shell/topbar";
import { TimeAgo } from "../ui/time_ago";
import { listSessionDispatches, readThreadCommands, threadObservationEntry } from "./client";
import { isDeliveryIssue, noticeKey } from "./command_notice_attention";
import { listCommandNoticeAcknowledgements } from "./command_notice_client";
import { storedCommandLabel, deliveryStateLabel, deliveryOperationLabel } from "./delivery_labels";
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
          receipt &&
            notice.receiptVersion !== undefined &&
            receipt.receiptVersion === notice.receiptVersion &&
            isDeliveryIssue(receipt.state)
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
            {original && <StoredCommandDetails receipt={original} />}
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

interface ArchivedExecutionFailure {
  notice: CommandNoticeRevision;
  acknowledgedAt: CommandNoticeAcknowledgement["acknowledgedAt"];
  command?: ThreadEntity;
}

async function dismissedExecutions(
  sessionId: string,
  pageToken: string,
  signal: AbortSignal
): Promise<Page<ArchivedExecutionFailure>> {
  const { acknowledgements, nextPageToken } = await listCommandNoticeAcknowledgements(
    sessionId,
    pageToken,
    signal,
    true
  );
  const commands = await readThreadCommands(
    sessionId,
    [...new Set(acknowledgements.flatMap(({ notice }) => (notice ? [notice.commandId] : [])))],
    signal
  );
  const byId = new Map(commands.map((row) => [row.entityId, row]));
  return {
    rows: acknowledgements.flatMap(({ notice, acknowledgedAt }) =>
      notice?.outcomeCursor === undefined ? [] : [{ notice, acknowledgedAt, command: byId.get(notice.commandId) }]
    ),
    nextPageToken,
  };
}

async function historyObservation(sessionId: string, cursor: bigint, signal: AbortSignal) {
  const response = await threadObservationEntry(
    create(ReadSessionEventsRequestSchema, { sessionId, afterCursor: cursor - 1n, limit: 1 }),
    signal
  );
  return response.entries[0]?.event?.observation;
}

function ReadFailureEvidence({ label, read }: { label: string; read: (signal: AbortSignal) => Promise<string> }) {
  const [result, setResult] = useState<{ text?: string; error?: string }>({});
  const [loading, setLoading] = useState(false);
  const current = useRef<AbortController | null>(null);
  useEffect(() => () => current.current?.abort(), []);
  const show = () => {
    const request = new AbortController();
    current.current = request;
    setLoading(true);
    read(request.signal)
      .then(
        (text) => {
          if (!request.signal.aborted) setResult({ text });
        },
        (error: unknown) => {
          if (!request.signal.aborted) setResult({ error: displayableError(error) });
        }
      )
      .finally(() => {
        if (!request.signal.aborted) setLoading(false);
      });
  };
  return (
    <Stack align="flex-start" gap={2}>
      {result.text !== undefined ? (
        <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
          {result.text}
        </Text>
      ) : (
        <Button size="compact-xs" variant="subtle" loading={loading} onClick={show}>
          {label}
        </Button>
      )}
      {result.error && (
        <Text size="xs" role="status">
          {result.error}
        </Text>
      )}
    </Stack>
  );
}

function DismissedExecutionFailures({ sessionId }: { sessionId: string }): JSX.Element {
  return (
    <PagedHistory
      sessionId={sessionId}
      load={dismissedExecutions}
      renderRows={(rows) =>
        rows.length === 0 ? (
          <Text size="sm" c="dimmed">
            No dismissed execution failures.
          </Text>
        ) : (
          rows.map(({ notice, acknowledgedAt, command }) => {
            const result = command?.entity.case === "command" ? command.entity.value : undefined;
            const exactFailure =
              result?.outcome === ThreadCommandOutcome.FAILED && result.outcomeCursor === notice.outcomeCursor;
            const stamp = timestampLabel(acknowledgedAt);
            return (
              <Stack key={`${notice.commandId}:${notice.outcomeCursor}`} gap={2} style={{ overflowWrap: "anywhere" }}>
                <Divider />
                <Text size="sm" fw={500}>
                  {result ? deliveryOperationLabel(result.operation) : notice.commandId} · Execution failed
                </Text>
                {stamp && (
                  <Text size="xs" c="dimmed">
                    Dismissed <TimeAgo value={stamp} />
                  </Text>
                )}
                {result?.requestedValue && <Text size="sm">{result.requestedValue}</Text>}
                {exactFailure ? (
                  <Text size="xs" style={{ whiteSpace: "pre-wrap" }}>
                    {result.outcomeReason || "No failure reason was recorded."}
                  </Text>
                ) : (
                  <ReadFailureEvidence
                    label="Show failure reason"
                    read={async (signal) => {
                      const outcome = await historyObservation(sessionId, notice.outcomeCursor!, signal);
                      if (outcome?.case !== "commandFailed" || outcome.value.commandId !== notice.commandId) {
                        throw new Error("History no longer has the exact failure.");
                      }
                      return outcome.value.reason || "No failure reason was recorded.";
                    }}
                  />
                )}
                {result?.inputRef && command && (
                  <ReadFailureEvidence
                    label="Show original command"
                    read={async (signal) => {
                      const admission = await historyObservation(sessionId, command.cursor, signal);
                      const original = admission?.case === "commandAdmitted" ? admission.value.command : undefined;
                      if (original?.commandId !== notice.commandId || original.operation.case !== "submitInput") {
                        throw new Error("History no longer has the original input.");
                      }
                      return original.operation.value.text;
                    }}
                  />
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
            <Tabs.Tab value="executions">Dismissed execution failures</Tabs.Tab>
          </Tabs.List>
          <Tabs.Panel value="receipts">
            <RetainedDispatches delivery={delivery} />
          </Tabs.Panel>
          <Tabs.Panel value="acknowledged">
            <AcknowledgedIssues delivery={delivery} />
          </Tabs.Panel>
          <Tabs.Panel value="executions">
            <DismissedExecutionFailures sessionId={delivery.sessionId} />
          </Tabs.Panel>
        </Tabs>
      )}
    </Modal>
  );
}
