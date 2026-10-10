import { type JSX, type ReactNode, useEffect, useRef, useState } from "react";
import { Alert, Badge, Button, Code, Paper, Stack, Text } from "@mantine/core";

import { displayableError } from "../client";
import { JsonView } from "../json_view";
import { StaleNotice, useOptionalStreamStatus } from "../stream_status";
import { followStream, type StreamConnection } from "../live_stream";
import { ActionCall } from "./call";
import { parseCallToolResult } from "./call_tool_result";
import {
  actionGroupService,
  actionService,
  type ActionGroupService,
  type ActionRequestView,
  type ActionService,
} from "./client";
import { renderDetailsResult } from "./rendering/index";
import { stateLabel } from "./requests";

/** The stored result: its pretty rendering unless it has none or the action is switched to Raw, and
 * otherwise its stored JSON. */
function ExecutionResult({
  result,
  pretty,
  raw,
}: {
  result: unknown;
  pretty: ReactNode | null;
  raw: boolean;
}): JSX.Element {
  return (
    <div>
      <Text size="sm" fw={600} mb={4}>
        Result
      </Text>
      {pretty === null || raw ? <JsonView value={result} /> : pretty}
    </div>
  );
}

/** A decided/terminal ActionRequest, kept as a durable receipt: the call as the operator saw it
 * when deciding, then the decision it is a record of and what came of it. */
export function ActionHistoryCard({ request, mcp }: { request: ActionRequestView; mcp: boolean }): JSX.Element {
  const decision = request.decision;
  const policySet = decision?.policy_evidence?.matched.policy_set;
  const [raw, setRaw] = useState(false);
  const result = request.execution?.result;
  const executionError = request.execution?.error;
  const hasResult = result !== null && result !== undefined;
  const hasExecutionError = executionError !== null && executionError !== undefined;
  const decisionState = request.state === "allowed" || request.state === "denied";
  // An MCP group's result is its stored CallToolResult, the same authority the Action Service answers
  // MCP callers by (`agentplane/action_service/tool_results.py`), drawn by the Action's own widget or
  // as the tool answered. Anything else, a sandbox group's own models or a group this page cannot
  // place, has no rendering but its JSON.
  const call = mcp && result !== null && result !== undefined ? parseCallToolResult(result) : null;
  const prettyResult = call === null ? null : renderDetailsResult(request.action, call);
  return (
    <Paper withBorder p="md">
      <Stack gap="sm">
        <ActionCall
          request={request}
          status={
            <>
              {decisionState && (
                <Text size="xs" c="dimmed">
                  {stateLabel(request.state)}
                </Text>
              )}
              {decision && (
                <Badge color={decision.verdict === "allow" ? "green" : "red"}>
                  {decision.verdict === "allow" ? "Allowed" : "Denied"}
                </Badge>
              )}
            </>
          }
          raw={raw}
          onRawChange={setRaw}
          prettyResult={prettyResult !== null}
        />
        {decision?.decision_note && <Text size="sm">{decision.decision_note}</Text>}
        {policySet && (
          <Text size="xs" c="dimmed">
            Auto-approved via policy <Code>{policySet}</Code>
          </Text>
        )}
        {(hasResult || hasExecutionError || !decisionState) && (
          <Stack gap="xs" data-testid="action-execution-outcome">
            {hasResult && <ExecutionResult result={result} pretty={prettyResult} raw={raw} />}
            {hasExecutionError && (
              <div>
                <Text size="sm" fw={600} mb={4}>
                  Execution error
                </Text>
                <JsonView value={executionError} />
              </div>
            )}
            {!decisionState && (
              <Text size="xs" c="dimmed" data-testid="action-execution-state">
                {stateLabel(request.state)}
              </Text>
            )}
          </Stack>
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
  embedded = false,
}: {
  service?: ActionService;
  groupService?: ActionGroupService;
  embedded?: boolean;
}): JSX.Element {
  const [requests, setRequests] = useState<ActionRequestView[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const loadedMore = useRef(false);
  const [connection, setConnection] = useState<StreamConnection | null>(null);
  const stream = useOptionalStreamStatus("Actions", connection);
  useEffect(() => {
    let active = true;
    let generation = 0;
    async function refresh(): Promise<void> {
      const current = ++generation;
      try {
        // Test services keep their existing list() contract; production never loads the full history.
        const page = service.history ? await service.history() : { items: await service.list(), next_cursor: null };
        if (!active || current !== generation) return;
        setRequests((previous) => {
          if (!loadedMore.current) return page.items;
          const older = previous.filter((item) => !page.items.some((newer) => newer.id === item.id));
          return [...page.items, ...older];
        });
        if (!loadedMore.current) setCursor(page.next_cursor);
        setError(null);
      } catch (failure) {
        if (active && current === generation) setError(displayableError(failure));
      } finally {
        if (active && current === generation) setLoading(false);
      }
    }
    const stop =
      service === actionService
        ? followStream("/actions/stream?state=decision_pending", {
            // The small pending snapshot also marks a successful resync after a renewed
            // upstream token or browser reconnect; changed hints alone are not replayable.
            events: { snapshot: () => void refresh() },
            onConnection: setConnection,
          })
        : undefined;
    void refresh();
    return () => {
      active = false;
      stop?.();
    };
  }, [service]);
  async function loadMore(): Promise<void> {
    if (!cursor || !service.history || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await service.history(cursor);
      loadedMore.current = true;
      setRequests((previous) => [
        ...previous,
        ...page.items.filter((item) => !previous.some((old) => old.id === item.id)),
      ]);
      setCursor(page.next_cursor);
    } catch (failure) {
      setError(displayableError(failure));
    } finally {
      setLoadingMore(false);
    }
  }
  const executors = useExecutorKinds(groupService);
  const decided = requests.filter((request) => request.state !== "decision_pending");

  return (
    <Stack>
      {embedded && (
        <Text fw={700} size="lg">
          History
        </Text>
      )}
      {!embedded && (
        <Text c="dimmed" size="sm">
          Denied and terminal ActionRequests, kept as durable receipts.
        </Text>
      )}
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
        <ActionHistoryCard
          key={request.id}
          request={request}
          mcp={executors.kinds?.get(request.action.group) === "mcp"}
        />
      ))}
      {cursor && (
        <Button data-testid="action-history-load-more" loading={loadingMore} onClick={() => void loadMore()}>
          Load more
        </Button>
      )}
    </Stack>
  );
}
