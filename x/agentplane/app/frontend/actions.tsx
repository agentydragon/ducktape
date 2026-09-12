import { Badge, Button, Code, Group, Paper, Stack, Text, Title } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";

import {
  actionService,
  serviceAccountKey,
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

export function JsonProjection({ value }: { value: unknown }): JSX.Element {
  return (
    <Code block style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
      {JSON.stringify(value, null, 2)}
    </Code>
  );
}

export function ExternalGrantDetails({
  grant,
}: {
  grant: NonNullable<ActionRequestView["external_grant"]>;
}): JSX.Element {
  return (
    <Stack gap={2} style={{ overflowWrap: "anywhere" }}>
      <Text size="xs" fw={600}>
        Authenticated external caller at submission
      </Text>
      <Text size="xs">
        Acts as <Code>{serviceAccountKey(grant.caller)}</Code> · Client <Code>{grant.client_id}</Code>
      </Text>
      <Text size="xs">
        Issuer <Code>{grant.issuer}</Code>
      </Text>
      <Text size="xs">
        Connection <Code>{grant.connection_id}</Code>
      </Text>
      <details>
        <Text component="summary" size="xs" style={{ cursor: "pointer" }}>
          Grant audit details
        </Text>
        <Stack gap={2} mt={4}>
          <Text size="xs">
            Grant <Code>{grant.grant_id}</Code> · Revision <Code>{grant.revision}</Code>
          </Text>
          <Text size="xs" c="dimmed">
            Historical submission evidence, not the connection’s current authorization status.
          </Text>
        </Stack>
      </details>
    </Stack>
  );
}

/** The caller's own plain-language framing of what it is asking for — the required one-line
 * `title` and the optional `description` — shared by the pending and history cards so the operator
 * reads the same words when deciding and when auditing. */
export function ActionContext({ request }: { request: ActionRequestView }): JSX.Element {
  return (
    <>
      {/* TODO: rendered verbatim as plain text; markdown rendering is a possible later addition. */}
      <Text size="sm">{request.title}</Text>
      {request.description && (
        <Text size="xs" c="dimmed">
          {request.description}
        </Text>
      )}
    </>
  );
}

function ActionCaller({ request }: { request: ActionRequestView }): JSX.Element {
  const grant = request.external_grant;
  if (!grant)
    return (
      <Text size="xs" c="dimmed">
        {request.caller_principal}
      </Text>
    );
  return <ExternalGrantDetails grant={grant} />;
}

/** Shared fetch/decide plumbing for the pending and history views: one live snapshot (the real
 * service pushes over `/actions/stream`) or one polled `list()` (any other service, e.g. tests). */
export function useActionRequests(service: ActionService): {
  requests: ActionRequestView[];
  error: string | null;
  loading: boolean;
  deciding: string | null;
  decide: (request: ActionRequestView, verdict: Verdict) => void;
} {
  const [requests, setRequests] = useState<ActionRequestView[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [deciding, setDeciding] = useState<string | null>(null);

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
    if (service !== actionService) {
      void refresh();
      return;
    }
    const source = new EventSource("/actions/stream");
    source.onopen = () => {
      setLoading(true);
      setError(null);
    };
    source.addEventListener("snapshot", (event) => {
      try {
        setRequests(JSON.parse((event as MessageEvent).data) as ActionRequestView[]);
        setError(null);
      } catch {
        setError("The live Action update was invalid.");
      } finally {
        setLoading(false);
      }
    });
    source.onerror = () => {
      setLoading(false);
      setError("The live Action stream disconnected; reconnecting.");
    };
    return () => source.close();
  }, [refresh, service]);

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

  return { requests, error, loading, deciding, decide: (request, verdict) => void decideRequest(request, verdict) };
}

function PendingActionCard({
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
        <Stack gap={2}>
          <Group justify="space-between" align="flex-start">
            <Text fw={600}>
              {request.action.group} / {request.action.name}
            </Text>
            <Badge color={STATE_COLORS[request.state] ?? "gray"}>{stateLabel(request.state)}</Badge>
          </Group>
          <ActionContext request={request} />
          <Text size="xs" c="dimmed">
            Request {request.id}
          </Text>
          <ActionCaller request={request} />
        </Stack>
        <div>
          <Text size="sm" fw={600} mb={4}>
            Exact arguments (unredacted)
          </Text>
          <JsonProjection value={request.arguments} />
        </div>
        <Group justify="flex-end">
          <Button color="red" variant="light" loading={deciding} onClick={() => onDecide(request, "deny")}>
            Deny
          </Button>
          <Button loading={deciding} onClick={() => onDecide(request, "allow")}>
            Allow
          </Button>
        </Group>
      </Stack>
    </Paper>
  );
}

/** The primary, actionable view: ActionRequests still awaiting an operator decision. Decided and
 * terminal requests live on the separate `ActionHistory` view (`actions_history.tsx`) instead of
 * alongside these. */
export function ActionRequests({ service = actionService }: { service?: ActionService }): JSX.Element {
  const { requests, error, loading, deciding, decide } = useActionRequests(service);
  const pending = requests.filter((request) => request.state === "decision_pending");

  return (
    <Stack>
      <div>
        <Title order={2}>Actions</Title>
        <Text c="dimmed" size="sm">
          Review pending ActionRequests. Allow dispatches the single permitted Execution automatically.
        </Text>
      </div>
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
