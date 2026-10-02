import { ActionIcon, Group, Paper, Stack, Text, Title } from "@mantine/core";
import IconCheck from "@tabler/icons-react/dist/esm/icons/IconCheck.mjs";
import IconX from "@tabler/icons-react/dist/esm/icons/IconX.mjs";
import { createContext, type JSX, useCallback, useContext, useEffect, useState } from "react";

import { displayableError } from "../client";
import { followStream, type StreamConnection } from "../live_stream";
import { StaleNotice, useStreamStatus, type StreamStatus } from "../stream_status";
import { ActionCall } from "./call";
import { actionService, type ActionRequestView, type ActionService, type ActionState, type Verdict } from "./client";

export function stateLabel(state: ActionState): string {
  return state.replaceAll("_", " ");
}

/** Shared fetch/decide plumbing for the pending and history views: one live snapshot (the real
 * service pushes over `/actions/stream`) or one polled `list()` (any other service, e.g. tests),
 * which has no `stream`. */
export function useActionRequests(
  service: ActionService,
  enabled = true
): {
  requests: ActionRequestView[];
  error: string | null;
  loading: boolean;
  stream: StreamStatus | null;
  deciding: string | null;
  decide: (request: ActionRequestView, verdict: Verdict) => void;
} {
  const [requests, setRequests] = useState<ActionRequestView[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [connection, setConnection] = useState<StreamConnection | null>(null);
  const [deciding, setDeciding] = useState<string | null>(null);
  const stream = useStreamStatus("Actions", connection);

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      setRequests(await service.list());
      setError(null);
    } catch (failure) {
      setError(displayableError(failure));
    } finally {
      setLoading(false);
    }
  }, [service]);

  useEffect(() => {
    if (!enabled) return;
    if (service !== actionService) {
      void refresh();
      return;
    }
    return followStream("/actions/stream?state=decision_pending", {
      events: {
        snapshot: (message) => {
          try {
            setRequests(JSON.parse(message.data) as ActionRequestView[]);
            setError(null);
          } catch {
            setError("The live Action update was invalid.");
          } finally {
            setLoading(false);
          }
        },
      },
      onConnection: setConnection,
    });
  }, [enabled, refresh, service]);

  async function decideRequest(request: ActionRequestView, verdict: Verdict): Promise<void> {
    setDeciding(request.id);
    try {
      const updated = await service.decide(request, verdict);
      setRequests((current) =>
        current.map((item) => (item.id === updated.id && updated.version >= item.version ? updated : item))
      );
      setError(null);
      if (service !== actionService) await refresh();
    } catch (failure) {
      setError(displayableError(failure));
    } finally {
      setDeciding(null);
    }
  }

  return {
    requests,
    error,
    loading,
    stream,
    deciding,
    decide: (request, verdict) => void decideRequest(request, verdict),
  };
}

export const ActionRequestsContext: ReturnType<typeof createContext<ReturnType<typeof useActionRequests> | null>> =
  createContext<ReturnType<typeof useActionRequests> | null>(null);

export function PendingActionCard({
  request,
  deciding,
  onDecide,
}: {
  request: ActionRequestView;
  deciding: boolean;
  onDecide: (request: ActionRequestView, verdict: Verdict) => void;
}): JSX.Element {
  const [raw, setRaw] = useState(false);
  return (
    <Paper withBorder p="md">
      <Stack gap="sm">
        <ActionCall
          request={request}
          headerActions={
            <Group gap="xs" wrap="nowrap">
              <ActionIcon
                size="md"
                variant="light"
                color="red"
                loading={deciding}
                aria-label="Deny"
                title="Deny"
                onClick={() => onDecide(request, "deny")}
              >
                <IconX size={18} />
              </ActionIcon>
              <ActionIcon
                size="md"
                variant="filled"
                loading={deciding}
                aria-label="Approve"
                title="Approve"
                onClick={() => onDecide(request, "allow")}
              >
                <IconCheck size={18} />
              </ActionIcon>
            </Group>
          }
          raw={raw}
          onRawChange={setRaw}
          prettyResult={false}
        />
      </Stack>
    </Paper>
  );
}

/** The primary, actionable view: ActionRequests still awaiting an operator decision. Decided and
 * terminal requests follow these in the unified Actions view. */
export function ActionRequests({
  service = actionService,
  embedded = false,
}: {
  service?: ActionService;
  embedded?: boolean;
}): JSX.Element {
  const shared = useContext(ActionRequestsContext);
  const local = useActionRequests(service, !(shared !== null && service === actionService));
  const { requests, error, loading, stream, deciding, decide } =
    shared !== null && service === actionService ? shared : local;
  const pending = requests.filter((request) => request.state === "decision_pending");

  return (
    <Stack>
      {!embedded && (
        <div>
          <Title order={2}>Actions</Title>
          <Text c="dimmed" size="sm">
            Review pending ActionRequests. Allow dispatches the single permitted Execution automatically.
          </Text>
        </div>
      )}
      <StaleNotice streams={[stream]} />
      {error && <Text c="red">{error}</Text>}
      {loading && <Text role="status">Loading actions…</Text>}
      <Title order={3}>{loading || error ? "Pending" : `Pending (${pending.length})`}</Title>
      {!loading && !error && pending.length === 0 && <Text c="dimmed">No requests are waiting for a decision.</Text>}
      {pending.map((request) => (
        <PendingActionCard key={request.id} request={request} deciding={deciding === request.id} onDecide={decide} />
      ))}
    </Stack>
  );
}
