import { Badge, Button, Code, Group, Paper, Stack, Text, Title } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";

import {
  actionService,
  displayableError,
  type ActionRequestView,
  type ActionService,
  type ActionState,
  type Verdict,
} from "./client";

const STATE_COLORS: Partial<Record<ActionState, string>> = {
  decision_pending: "yellow",
  allowed: "blue",
  denied: "red",
  dispatching: "cyan",
  running: "cyan",
  succeeded: "green",
  failed: "red",
  cancelled: "gray",
  execution_unknown: "orange",
};

export function stateLabel(state: ActionState): string {
  return state.replaceAll("_", " ");
}

function JsonProjection({ value }: { value: unknown }): JSX.Element {
  return (
    <Code block style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
      {JSON.stringify(value, null, 2)}
    </Code>
  );
}

function ActionCard({
  request,
  deciding,
  onDecide,
}: {
  request: ActionRequestView;
  deciding: boolean;
  onDecide: (request: ActionRequestView, verdict: Verdict) => void;
}): JSX.Element {
  return (
    <Paper withBorder p="md">
      <Stack gap="sm">
        <Group justify="space-between" align="flex-start">
          <Stack gap={2}>
            <Text fw={600}>{request.capability}</Text>
            <Text size="xs" c="dimmed">
              Request {request.id} · thread {request.origin_thread_id}
            </Text>
            <Text size="xs" c="dimmed">
              {request.caller_kind} · {request.caller_principal}
            </Text>
          </Stack>
          <Badge color={STATE_COLORS[request.state] ?? "gray"}>{stateLabel(request.state)}</Badge>
        </Group>
        <div>
          <Text size="sm" fw={600} mb={4}>
            Safe argument projection
          </Text>
          <JsonProjection value={request.arguments} />
        </div>
        {request.decision && (
          <Text size="sm">
            Decision: <b>{request.decision.verdict}</b> by {request.decision.issuer}
            {request.decision.reason ? ` · ${request.decision.reason}` : ""}
          </Text>
        )}
        {request.execution?.result !== null && request.execution?.result !== undefined && (
          <div>
            <Text size="sm" fw={600} mb={4}>
              Result
            </Text>
            <JsonProjection value={request.execution.result} />
          </div>
        )}
        {request.execution?.error && (
          <div>
            <Text size="sm" fw={600} mb={4}>
              Execution error
            </Text>
            <JsonProjection value={request.execution.error} />
          </div>
        )}
        {request.state === "decision_pending" && (
          <Group justify="flex-end">
            <Button color="red" variant="light" loading={deciding} onClick={() => onDecide(request, "deny")}>
              Deny
            </Button>
            <Button loading={deciding} onClick={() => onDecide(request, "allow")}>
              Allow
            </Button>
          </Group>
        )}
      </Stack>
    </Paper>
  );
}

export function ActionRequests({ service = actionService }: { service?: ActionService }): JSX.Element {
  const [requests, setRequests] = useState<ActionRequestView[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [deciding, setDeciding] = useState<string | null>(null);

  const refresh = useCallback(async (): Promise<void> => {
    try {
      setRequests(await service.list());
      setError(null);
    } catch (failure) {
      setError(displayableError(failure));
    }
  }, [service]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function decide(request: ActionRequestView, verdict: Verdict): Promise<void> {
    setDeciding(request.id);
    try {
      const updated = await service.decide(request, verdict);
      setRequests((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      setError(null);
      await refresh();
    } catch (failure) {
      setError(displayableError(failure));
    } finally {
      setDeciding(null);
    }
  }

  const pending = requests.filter((request) => request.state === "decision_pending");
  const decided = requests.filter((request) => request.state !== "decision_pending");

  return (
    <Stack>
      <div>
        <Title order={2}>Actions</Title>
        <Text c="dimmed" size="sm">
          Review pending ActionRequests. Allow dispatches the single permitted Execution automatically; denied and
          terminal requests remain visible as durable receipts.
        </Text>
      </div>
      {error && <Text c="red">{error}</Text>}
      <Title order={3}>Pending ({pending.length})</Title>
      {pending.length === 0 && <Text c="dimmed">No requests are waiting for a decision.</Text>}
      {pending.map((request) => (
        <ActionCard
          key={request.id}
          request={request}
          deciding={deciding === request.id}
          onDecide={(item, verdict) => void decide(item, verdict)}
        />
      ))}
      <Title order={3}>Recent requests</Title>
      {decided.length === 0 && <Text c="dimmed">No decided requests yet.</Text>}
      {decided.map((request) => (
        <ActionCard
          key={request.id}
          request={request}
          deciding={deciding === request.id}
          onDecide={(item, verdict) => void decide(item, verdict)}
        />
      ))}
    </Stack>
  );
}
