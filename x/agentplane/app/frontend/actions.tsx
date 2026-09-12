import { Accordion, Badge, Button, Code, Group, Paper, Stack, Text, Title } from "@mantine/core";
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

function JsonProjection({ value }: { value: unknown }): JSX.Element {
  return (
    <Code block style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
      {JSON.stringify(value, null, 2)}
    </Code>
  );
}

function ExternalGrantDetails({ grant }: { grant: NonNullable<ActionRequestView["external_grant"]> }): JSX.Element {
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
function useActionRequests(service: ActionService): {
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

/** A decided/terminal ActionRequest, kept as a durable receipt: the decision it's a record of leads
 * the card, with the exact arguments folded behind a disclosure rather than shown unconditionally
 * (the pending card's job is a live decision, this one's is a compact audit trail). */
function HistoryCard({ request }: { request: ActionRequestView }): JSX.Element {
  const decision = request.decision;
  const policySet = decision?.policy_evidence?.matched.policy_set;
  return (
    <Paper withBorder p="md">
      <Stack gap="sm">
        <Stack gap={4}>
          <Group gap="xs" wrap="wrap" align="center">
            <Text fw={600} ff="monospace">
              {request.action.group} / {request.action.name}
            </Text>
            <Text size="sm" c="dimmed">
              requested by {request.caller_principal}
            </Text>
            {decision && (
              <Badge color={decision.verdict === "allow" ? "green" : "red"} style={{ marginLeft: "auto" }}>
                {decision.verdict === "allow" ? "Allowed" : "Denied"}
              </Badge>
            )}
          </Group>
          <Text size="xs" c="dimmed">
            Request {request.id} · {stateLabel(request.state)}
          </Text>
          {request.external_grant && <ExternalGrantDetails grant={request.external_grant} />}
        </Stack>
        {decision?.decision_note && <Text size="sm">{decision.decision_note}</Text>}
        {policySet && (
          <Text size="xs" c="dimmed">
            Auto-approved via policy <Code>{policySet}</Code>
          </Text>
        )}
        <Accordion variant="contained" chevronPosition="left" classNames={{ chevron: "agentplane-accordion-chevron" }}>
          <Accordion.Item value="arguments">
            <Accordion.Control>Arguments</Accordion.Control>
            <Accordion.Panel>
              <JsonProjection value={request.arguments} />
            </Accordion.Panel>
          </Accordion.Item>
        </Accordion>
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
      </Stack>
    </Paper>
  );
}

/** The primary, actionable view: ActionRequests still awaiting an operator decision. Decided and
 * terminal requests live on the separate `ActionHistory` view instead of alongside these. */
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

/** Decided and terminal ActionRequests: durable receipts, not something an operator still acts on. */
export function ActionHistory({ service = actionService }: { service?: ActionService }): JSX.Element {
  const { requests, error, loading } = useActionRequests(service);
  const decided = requests.filter((request) => request.state !== "decision_pending");

  return (
    <Stack>
      <div>
        <Title order={2}>Action history</Title>
        <Text c="dimmed" size="sm">
          Denied and terminal ActionRequests, kept as durable receipts.
        </Text>
      </div>
      {error && <Text c="red">{error}</Text>}
      {loading && <Text role="status">Loading actions…</Text>}
      {!loading && !error && decided.length === 0 && <Text c="dimmed">No decided requests yet.</Text>}
      {decided.map((request) => (
        <HistoryCard key={request.id} request={request} />
      ))}
    </Stack>
  );
}
