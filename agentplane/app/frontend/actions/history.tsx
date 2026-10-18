import { type JSX, type ReactNode, useEffect, useRef, useState } from "react";
import { Alert, Badge, Button, Code, Paper, SegmentedControl, Stack, Text } from "@mantine/core";

import { displayableError } from "../client";
import { ClampedBlock, lineCount } from "../clamped_block";
import { OUTPUT_MAX_HEIGHT_REM } from "../command_view";
import { ActionJson } from "./payload";
import { useOptionalStreamStatus } from "../stream_status";
import { followStream, type StreamConnection } from "../live_stream";
import { ActionCall } from "./call";
import { parseCallToolResult } from "./call_tool_result";
import { actionHistory, listActionGroups, type ActionRequestView } from "./client";
import { renderActionCall, renderDetailsResult, renderNativeDetailsResult } from "./rendering/index";
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
      {pretty === null || raw ? <ActionJson value={result} /> : pretty}
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
  const call = mcp && hasResult ? parseCallToolResult(result) : null;
  const prettyResult = mcp
    ? call === null
      ? null
      : renderDetailsResult(request.action, call)
    : hasResult
      ? renderNativeDetailsResult(request.action, result)
      : null;
  const combinedCall = renderActionCall(request.action, request.arguments, call ?? undefined);
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
          combinedCall={combinedCall}
        />
        {decision?.decision_note && (
          <ClampedBlock
            maxHeightRem={OUTPUT_MAX_HEIGHT_REM}
            lines={lineCount(decision.decision_note)}
            label="Decision note"
          >
            <Text size="sm">{decision.decision_note}</Text>
          </ClampedBlock>
        )}
        {policySet && (
          <Text size="xs" c="dimmed">
            Auto-approved via policy <Code>{policySet}</Code>
          </Text>
        )}
        {(hasResult || hasExecutionError || !decisionState) && (
          <Stack gap="xs" data-testid="action-execution-outcome">
            {hasResult && !(combinedCall?.includesResult && !raw) && (
              <ExecutionResult result={result} pretty={prettyResult} raw={raw} />
            )}
            {hasExecutionError && (
              <div>
                <Text size="sm" fw={600} mb={4}>
                  Execution error
                </Text>
                <ActionJson value={executionError} />
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
function useExecutorKinds(): {
  kinds: ReadonlyMap<string, string> | null;
  error: string | null;
} {
  const [kinds, setKinds] = useState<ReadonlyMap<string, string> | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void listActionGroups().then(
      (groups) => setKinds(new Map(groups.map((group) => [group.key, group.executor_kind]))),
      (failure: unknown) => setError(displayableError(failure))
    );
  }, []);
  return { kinds, error };
}

/** Decided and terminal ActionRequests: durable receipts, not something an operator still acts on. */
function ActionHistoryRequests({ includeAutoAllowed }: { includeAutoAllowed: boolean }): JSX.Element {
  const [requests, setRequests] = useState<ActionRequestView[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const loadedMore = useRef(false);
  const [connection, setConnection] = useState<StreamConnection | null>(null);
  useOptionalStreamStatus("Actions", connection);
  useEffect(() => {
    let active = true;
    let generation = 0;
    async function refresh(): Promise<void> {
      const current = ++generation;
      try {
        const page = await actionHistory(undefined, includeAutoAllowed);
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
    const stop = followStream("/actions/stream?state=decision_pending", {
      // The small pending snapshot also marks a successful resync after a renewed
      // upstream token or browser reconnect; changed hints alone are not replayable.
      events: { snapshot: () => void refresh() },
      onConnection: setConnection,
    });
    void refresh();
    return () => {
      active = false;
      stop();
    };
  }, [includeAutoAllowed]);
  async function loadMore(): Promise<void> {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await actionHistory(cursor, includeAutoAllowed);
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
  const executors = useExecutorKinds();
  const decided = requests.filter((request) => request.state !== "decision_pending");

  return (
    <Stack>
      {error && <Text c="red">{error}</Text>}
      {executors.error && <Alert color="orange">Action group metadata could not be loaded. {executors.error}</Alert>}
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

export function ActionHistory({ embedded = false }: { embedded?: boolean }): JSX.Element {
  const [filter, setFilter] = useState("not-auto-allowed");
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
      <SegmentedControl
        aria-label="Action history filter"
        value={filter}
        onChange={setFilter}
        data={[
          { value: "not-auto-allowed", label: "Not auto-allowed" },
          { value: "all", label: "All actions" },
        ]}
      />
      <ActionHistoryRequests key={filter} includeAutoAllowed={filter === "all"} />
    </Stack>
  );
}
