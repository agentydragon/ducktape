import { type JSX, useEffect, useState } from "react";
import { Accordion, Alert, Badge, Code, Group, Paper, Stack, Text, Title } from "@mantine/core";

import { ActionCaller, ActionContext, RequestAuditDetails, stateLabel, useActionRequests } from "./actions";
import { type CallToolResult, CallToolResultView, parseCallToolResult } from "./call_tool_result";
import {
  actionGroupService,
  actionService,
  displayableError,
  type ActionGroupService,
  type ActionRequestView,
  type ActionService,
} from "./client";
import { JsonView } from "./json_view";
import { RawSwitch } from "./raw_switch";
import { StaleNotice } from "./stream_status";

import "./actions_history.css";

/** The stored result: drawn as the tool answered when `call` holds it decoded, with its own Raw switch
 * to the stored JSON, and otherwise only that JSON. */
function ExecutionResult({ result, call }: { result: unknown; call: CallToolResult | null }): JSX.Element {
  const [raw, setRaw] = useState(false);
  return (
    <div>
      <Group gap="sm" mb={4}>
        <Text size="sm" fw={600}>
          Result
        </Text>
        {call !== null && <RawSwitch raw={raw} onChange={setRaw} />}
      </Group>
      {call !== null && !raw ? <CallToolResultView result={call} /> : <JsonView value={result} />}
    </div>
  );
}

/** A decided/terminal ActionRequest, kept as a durable receipt: the decision it's a record of leads
 * the card, with the exact arguments folded behind a disclosure rather than shown unconditionally
 * (the pending card's job is a live decision, this one's is a compact audit trail). */
function HistoryCard({ request, mcp }: { request: ActionRequestView; mcp: boolean }): JSX.Element {
  const decision = request.decision;
  const policySet = decision?.policy_evidence?.matched.policy_set;
  const result = request.execution?.result;
  // An MCP group's result is its stored CallToolResult, drawn as the tool answered, the same authority
  // the Action Service answers MCP callers by (`agentplane/action_service/tool_results.py`). Anything
  // else, a sandbox group's own models or a group this page cannot place, has no rendering but its JSON.
  const call = mcp && result !== null && result !== undefined ? parseCallToolResult(result) : null;
  return (
    <Paper withBorder p="md">
      <Stack gap="sm">
        <Stack gap={4}>
          <Group gap="xs" wrap="wrap" align="center">
            <Text fw={600} ff="monospace">
              {request.action.group} / {request.action.name}
            </Text>
            <Text size="xs" c="dimmed">
              requested by {request.caller && `${request.caller.namespace}/${request.caller.name}`}
            </Text>
            {decision && (
              <Badge color={decision.verdict === "allow" ? "green" : "red"} style={{ marginLeft: "auto" }}>
                {decision.verdict === "allow" ? "Allowed" : "Denied"}
              </Badge>
            )}
          </Group>
          <ActionContext request={request} />
          <Text size="xs" c="dimmed">
            {stateLabel(request.state)}
          </Text>
          {request.external_grant && <ActionCaller request={request} />}
          <RequestAuditDetails request={request} />
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
              <JsonView value={request.arguments} />
            </Accordion.Panel>
          </Accordion.Item>
        </Accordion>
        {result !== null && result !== undefined && <ExecutionResult result={result} call={call} />}
        {request.execution?.error && (
          <div>
            <Text size="sm" fw={600} mb={4}>
              Execution error
            </Text>
            <JsonView value={request.execution.error} />
          </div>
        )}
      </Stack>
    </Paper>
  );
}

/** Each configured Action group's executor kind, by group key: `null` until the groups arrive, and
 * for good when they cannot be read, which `error` then says. */
function useExecutorKinds(groupService: ActionGroupService): {
  kinds: ReadonlyMap<string, string> | null;
  error: string | null;
} {
  const [kinds, setKinds] = useState<ReadonlyMap<string, string> | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void groupService.list().then(
      (groups) => setKinds(new Map(groups.map((group) => [group.key, group.executor_kind]))),
      (failure: unknown) => setError(displayableError(failure))
    );
  }, [groupService]);
  return { kinds, error };
}

/** Decided and terminal ActionRequests: durable receipts, not something an operator still acts on. */
export function ActionHistory({
  service = actionService,
  groupService = actionGroupService,
}: {
  service?: ActionService;
  groupService?: ActionGroupService;
}): JSX.Element {
  const { requests, error, loading, stream } = useActionRequests(service);
  const executors = useExecutorKinds(groupService);
  const decided = requests.filter((request) => request.state !== "decision_pending");

  return (
    <Stack>
      <div>
        <Title order={2}>Action history</Title>
        <Text c="dimmed" size="sm">
          Denied and terminal ActionRequests, kept as durable receipts.
        </Text>
      </div>
      <StaleNotice streams={[stream]} />
      {error && <Text c="red">{error}</Text>}
      {executors.error && (
        <Alert color="orange">
          Results show as their stored JSON: the Action groups could not be loaded. {executors.error}
        </Alert>
      )}
      {loading && <Text role="status">Loading actions…</Text>}
      {!loading && !error && decided.length === 0 && <Text c="dimmed">No decided requests yet.</Text>}
      {decided.map((request) => (
        <HistoryCard key={request.id} request={request} mcp={executors.kinds?.get(request.action.group) === "mcp"} />
      ))}
    </Stack>
  );
}
