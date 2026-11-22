import { Button, Divider, Group, Modal, ScrollArea, Stack, Text } from "@mantine/core";
import { type JSX, type ReactNode, useEffect, useState } from "react";
import { Operation } from "../../../proto/command_pb";
import { DeliveryState, type Dispatch } from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { displayableError } from "../transport/client";
import { TopbarActions } from "../shell/topbar";
import { TimeAgo } from "../ui/time_ago";
import { listSessionDispatches } from "./client";
import { StoredCommandDetails } from "./stored_command_details";
import { timestampLabel } from "./thread_presentation";

export function deliveryOperationLabel(operation: Operation): string {
  switch (operation) {
    case Operation.SUBMIT_INPUT:
      return "Send message";
    case Operation.CHANGE_MODEL:
      return "Model change";
    case Operation.CHANGE_REASONING_EFFORT:
      return "Reasoning effort change";
    case Operation.INTERRUPT_TURN:
      return "Turn interrupt";
    case Operation.COMPACT:
      return "Compact conversation";
    default:
      return "Command";
  }
}

export const deliveryStateLabel: Record<DeliveryState, string> = {
  [DeliveryState.UNSPECIFIED]: "Delivery unconfirmed",
  [DeliveryState.QUEUED_IN_DISPATCHER]: "Queued in Dispatcher",
  [DeliveryState.DELIVERING_TO_RUNNER]: "Sending to runner",
  [DeliveryState.RUNNER_ADMITTED]: "Runner accepted",
  [DeliveryState.FAILED]: "Delivery failed",
  [DeliveryState.CANCELLED_IN_DISPATCHER]: "Cancelled before delivery",
  [DeliveryState.UNKNOWN]: "Outcome unknown",
};

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
        renderRows={(rows) =>
          rows.length === 0 ? (
            <Text size="sm" c="dimmed">
              No retained receipts.
            </Text>
          ) : (
            rows.map((row) => {
              const time = timestampLabel(row.updatedAt);
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
                  <StoredCommandDetails receipt={row} />
                </Stack>
              );
            })
          )
        }
      />
    </Stack>
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
  opened,
  onClose,
}: {
  sessionId: string;
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
      {opened && <RetainedDispatches key={sessionId} sessionId={sessionId} />}
    </Modal>
  );
}
