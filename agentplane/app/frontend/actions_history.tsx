import { type JSX, useEffect, useState } from "react";
import { Alert, Badge, Code, Paper, Stack, Text, Title } from "@mantine/core";

import { ActionCall } from "./action_call";
import { stateLabel, useActionRequests } from "./actions";
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
import { StaleNotice } from "./stream_status";

/** The stored result: drawn as the tool answered when `call` holds it decoded and the action is not
 * switched to Raw, and otherwise its stored JSON. */
function ExecutionResult({
  result,
  call,
  raw,
}: {
  result: unknown;
  call: CallToolResult | null;
  raw: boolean;
}): JSX.Element {
  return (
    <div>
      <Text size="sm" fw={600} mb={4}>
        Result
      </Text>
      {call !== null && !raw ? <CallToolResultView result={call} /> : <JsonView value={result} />}
    </div>
  );
}

/** A decided/terminal ActionRequest, kept as a durable receipt: the call as the operator saw it
 * when deciding, then the decision it is a record of and what came of it. */
function HistoryCard({ request, mcp }: { request: ActionRequestView; mcp: boolean }): JSX.Element {
  const decision = request.decision;
  const policySet = decision?.policy_evidence?.matched.policy_set;
  const [raw, setRaw] = useState(false);
  const result = request.execution?.result;
  // An MCP group's result is its stored CallToolResult, drawn as the tool answered, the same authority
  // the Action Service answers MCP callers by (`agentplane/action_service/tool_results.py`). Anything
  // else, a sandbox group's own models or a group this page cannot place, has no rendering but its JSON.
  const call = mcp && result !== null && result !== undefined ? parseCallToolResult(result) : null;
  return (
    <Paper withBorder p="md">
      <Stack gap="sm">
        <ActionCall
          request={request}
          status={
            <>
              <Text size="xs" c="dimmed">
                {stateLabel(request.state)}
              </Text>
              {decision && (
                <Badge color={decision.verdict === "allow" ? "green" : "red"}>
                  {decision.verdict === "allow" ? "Allowed" : "Denied"}
                </Badge>
              )}
            </>
          }
          raw={raw}
          onRawChange={setRaw}
          prettyResult={call !== null}
        />
        {decision?.decision_note && <Text size="sm">{decision.decision_note}</Text>}
        {policySet && (
          <Text size="xs" c="dimmed">
            Auto-approved via policy <Code>{policySet}</Code>
          </Text>
        )}
        {result !== null && result !== undefined && <ExecutionResult result={result} call={call} raw={raw} />}
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
