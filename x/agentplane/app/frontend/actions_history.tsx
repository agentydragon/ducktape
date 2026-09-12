import { Accordion, Badge, Code, Group, Paper, Stack, Text, Title } from "@mantine/core";

import { ExternalGrantDetails, JsonProjection, stateLabel, useActionRequests } from "./actions";
import { actionService, type ActionRequestView, type ActionService } from "./client";

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
