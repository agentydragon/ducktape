import { type JSX, type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Code,
  Group,
  Paper,
  SegmentedControl,
  Stack,
  Text,
  Tooltip,
} from "@mantine/core";

import { Link, useLocation, useSearchParams } from "react-router";

import { Disclosure, DisclosureBody } from "../disclosure";
import { durationLabel } from "../format_time";
import { IconArrowRight } from "../icons";
import { RawSwitch } from "../raw_switch";
import { TimeAgo } from "../time_ago";
import { displayableError } from "../client";
import { ClampedBlock, lineCount, TEXT_MAX_HEIGHT_REM } from "../clamped_block";
import { JsonBlock } from "../text_block";
import { useOptionalStreamStatus } from "../stream_status";
import type { StreamConnection } from "../stream_reader";
import { ActionCall } from "./call";
import { CallToolResultView, parseCallToolResult, type CallToolResult } from "./call_tool_result";
import { actionHistory, followActionRequests, getAction, listActionGroups, type ActionRequestView } from "./client";
import {
  renderActionCall,
  renderActionOutcome,
  renderActionLabel,
  renderDetailsArguments,
  renderDetailsResult,
  renderNativeDetailsResult,
} from "./rendering/index";
import { stateLabel } from "./requests";
import type { DecisionView } from "./types";

/** The stored result: its pretty rendering unless it has none or the action is switched to Raw, and
 * otherwise its stored JSON. */
function ExecutionResult({
  result,
  call,
  pretty,
  raw,
}: {
  result: unknown;
  call: CallToolResult | null;
  pretty: ReactNode | null | undefined;
  raw: boolean;
}): JSX.Element | null {
  if (pretty === null && !raw) return null;
  return (
    <div>
      <Text size="sm" fw={600} mb={4}>
        Result
      </Text>
      {raw ? (
        <JsonBlock value={result} />
      ) : pretty === undefined ? (
        call === null ? (
          <JsonBlock value={result} />
        ) : (
          <CallToolResultView result={call} />
        )
      ) : (
        pretty
      )}
    </div>
  );
}

/** Who decided and how: an operator after how long a wait, or the policy that auto-approved. `named`
 * when the status already names the decision, as it does until the request runs. */
function DecisionSummary({
  decision,
  requestedAt,
  named,
}: {
  decision: DecisionView;
  requestedAt: string;
  named: boolean;
}): JSX.Element {
  const verb = named
    ? "Decided"
    : decision.verdict === "deny"
      ? "Denied"
      : decision.operator === null
        ? "Auto-approved"
        : "Approved";
  if (decision.operator === null) {
    const policySet = decision.policy_evidence?.matched.policy_set;
    return (
      <>
        {verb}
        {policySet && (
          <>
            {" "}
            via policy <Code>{policySet}</Code>
          </>
        )}
      </>
    );
  }
  const wait = durationLabel(Math.round((Date.parse(decision.decided_at) - Date.parse(requestedAt)) / 1000));
  return <>{wait === "" ? verb : `${verb} after ${wait}`}</>;
}

/** A decided/terminal ActionRequest, kept as a durable receipt: the call as the operator saw it
 * when deciding, then the decision it is a record of and what came of it. */
export function ActionHistoryCard({
  request,
  mcp,
  compact = false,
}: {
  request: ActionRequestView;
  mcp: boolean;
  compact?: boolean;
}): JSX.Element {
  const location = useLocation();
  const decision = request.decision;
  const [raw, setRaw] = useState(false);
  const result = request.execution?.result;
  const executionError = request.execution?.error;
  const hasResult = result !== null && result !== undefined;
  const hasExecutionError = executionError !== null && executionError !== undefined;
  const decisionState = request.state === "allowed" || request.state === "denied";
  const call = mcp && hasResult ? parseCallToolResult(result) : null;
  const prettyResult = mcp
    ? call === null
      ? undefined
      : renderDetailsResult(request.action, call)
    : hasResult
      ? renderNativeDetailsResult(request.action, result)
      : undefined;
  const combinedCall = renderActionCall(request.action, request.arguments, call ?? undefined);
  const actionOutcome = call === null ? undefined : renderActionOutcome(request.action, call);
  const toolError = request.state === "succeeded" && call?.isError === true;
  const decisionLabel =
    decision === null
      ? undefined
      : decision.verdict === "allow"
        ? decision.operator === null
          ? "Auto-allowed"
          : "Allowed"
        : "Denied";
  const outcomeLabel = toolError
    ? "Tool error"
    : (actionOutcome?.label ??
      (decisionState ? (decisionLabel ?? stateLabel(request.state)) : stateLabel(request.state)));
  const failure =
    ["failed", "cancelled", "execution_unknown"].includes(request.state) || toolError || actionOutcome?.failed === true;
  const [receiptOpen, setReceiptOpen] = useState(failure);
  useEffect(() => {
    if (failure) setReceiptOpen(true);
  }, [failure]);
  function changeRaw(next: boolean): void {
    setRaw(next);
    if (next) setReceiptOpen(true);
  }
  const color = failure
    ? "red"
    : request.state === "succeeded"
      ? "green"
      : request.state === "denied"
        ? "gray"
        : "blue";
  const status = (
    <Badge color={color} data-testid="action-execution-state">
      {outcomeLabel}
    </Badge>
  );
  const timestamp = (
    <Text size="xs" c="dimmed">
      {decision && (
        <>
          <DecisionSummary decision={decision} requestedAt={request.created_at} named={decisionState} />
          {" · "}
        </>
      )}
      {request.execution?.completed_at ? "Completed " : "Updated "}
      <TimeAgo value={request.execution?.completed_at ?? request.updated_at} />
    </Text>
  );
  const pretty =
    call !== null ||
    prettyResult !== undefined ||
    renderDetailsArguments(request.action, request.arguments) !== undefined ||
    combinedCall !== undefined;
  const actionLabel = renderActionLabel(request.action, request.arguments);
  const receipt = (
    <Stack className="agentplane-action-operation-host" data-operation-density="detail" gap="sm">
      <ActionCall
        request={request}
        status={status}
        headerActions={timestamp}
        raw={raw}
        onRawChange={changeRaw}
        prettyResult={call !== null || prettyResult !== undefined}
        combinedCall={combinedCall}
        detailsOnly={compact}
      />
      {decision?.decision_note && (
        <ClampedBlock
          maxHeightRem={TEXT_MAX_HEIGHT_REM}
          lines={lineCount(decision.decision_note)}
          label="Decision note"
        >
          <Text size="sm">{decision.decision_note}</Text>
        </ClampedBlock>
      )}
      {(hasResult || hasExecutionError || !decisionState) && (
        <Stack gap="xs" data-testid="action-execution-outcome">
          {hasResult && !(combinedCall?.includesResult && !raw) && (
            <ExecutionResult result={result} call={call} pretty={prettyResult} raw={raw} />
          )}
          {hasExecutionError && (
            <div>
              <Text size="sm" fw={600} mb={4}>
                Execution error
              </Text>
              <JsonBlock value={executionError} />
            </div>
          )}
        </Stack>
      )}
    </Stack>
  );
  return (
    <Paper withBorder p="md" data-action-receipt={request.id}>
      {compact ? (
        <Stack gap="xs">
          <Stack className="agentplane-action-history-compact-overview" gap="xs">
            <Group justify="space-between" align="flex-start" gap="xs">
              <div>
                {actionLabel === undefined ? (
                  <Text fw={600}>
                    {request.action.group} / {request.action.name}
                  </Text>
                ) : (
                  actionLabel
                )}
              </div>
              <Group gap="xs">
                {status}
                {pretty && <RawSwitch raw={raw} onChange={changeRaw} />}
                <Tooltip label="View full details" withArrow>
                  <ActionIcon
                    component={Link}
                    to={`/actions/${request.id}`}
                    state={{ returnTo: location }}
                    variant="subtle"
                    color="gray"
                    aria-label={`View details for ${request.title}`}
                  >
                    <IconArrowRight size={15} aria-hidden="true" />
                  </ActionIcon>
                </Tooltip>
              </Group>
            </Group>
            <Text size="sm" style={{ overflowWrap: "anywhere" }}>
              {request.title}
            </Text>
            {timestamp}
          </Stack>
          <Disclosure summary="Receipt details" open={receiptOpen} onOpenChange={setReceiptOpen} keepMounted>
            <DisclosureBody>{receipt}</DisclosureBody>
          </Disclosure>
        </Stack>
      ) : (
        receipt
      )}
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
function ActionHistoryRequests({
  includeAutoAllowed,
  showAll,
}: {
  includeAutoAllowed: boolean;
  showAll: () => void;
}): JSX.Element {
  const [requests, setRequests] = useState<ActionRequestView[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [pageError, setPageError] = useState<string | null>(null);
  const rows = useRef<ActionRequestView[]>([]);
  const loadedMore = useRef(false);
  const pageInFlight = useRef(false);
  const active = useRef(true);
  const sentinel = useRef<HTMLDivElement>(null);
  const [connection, setConnection] = useState<StreamConnection | null>(null);
  useOptionalStreamStatus("Actions", connection);
  const replaceRows = useCallback((update: (previous: ActionRequestView[]) => ActionRequestView[]): void => {
    setRequests((previous) => {
      const next = update(previous);
      rows.current = next;
      return next;
    });
  }, []);
  useEffect(() => {
    active.current = true;
    let generation = 0;
    let refreshing = false;
    let refreshAgain = false;
    async function refresh(): Promise<void> {
      if (refreshing) {
        refreshAgain = true;
        return;
      }
      refreshing = true;
      const current = ++generation;
      try {
        // The first page does not contain every still-running receipt after paging.
        // Refresh those retained rows by identity as well, without rereading all history.
        const page = await actionHistory(undefined, includeAutoAllowed);
        const pageIds = new Set(page.items.map((row) => row.id));
        const unfinished = rows.current.filter(
          (row) => !pageIds.has(row.id) && ["allowed", "dispatching", "running"].includes(row.state)
        );
        const updates = await Promise.all(unfinished.map((row) => getAction(row.id)));
        if (!active.current || current !== generation) return;
        replaceRows((previous) => {
          const fresh = new Map([...updates, ...page.items].map((row) => [row.id, row]));
          // A page response can race with a newer receipt already seen on another request.
          for (const row of previous) {
            const candidate = fresh.get(row.id);
            if (candidate && row.version > candidate.version) fresh.set(row.id, row);
          }
          const latest = page.items.map((row) => fresh.get(row.id)!);
          if (!loadedMore.current) return latest;
          const firstPageIds = new Set(page.items.map((row) => row.id));
          const older = previous.filter((row) => !firstPageIds.has(row.id));
          return [...latest, ...older.map((row) => fresh.get(row.id) ?? row)];
        });
        if (!loadedMore.current) setCursor(page.next_cursor);
        setError(null);
      } catch (failure) {
        if (active.current && current === generation) setError(displayableError(failure));
      } finally {
        refreshing = false;
        if (active.current && current === generation) {
          setLoading(false);
          if (refreshAgain) {
            refreshAgain = false;
            void refresh();
          }
        }
      }
    }
    const stop = followActionRequests({
      onOpen: () => undefined,
      onFrame: (frame) => {
        if (frame.kind === "snapshot_complete" || frame.kind === "upsert") void refresh();
      },
      onConnection: setConnection,
    });
    void refresh();
    return () => {
      active.current = false;
      generation++;
      stop();
    };
  }, [includeAutoAllowed, replaceRows]);
  const loadMore = useCallback(async (): Promise<void> => {
    // A ref closes the gap before React commits loadingMore, so one cursor has one request.
    if (!active.current || !cursor || pageInFlight.current) return;
    pageInFlight.current = true;
    setLoadingMore(true);
    setPageError(null);
    try {
      const page = await actionHistory(cursor, includeAutoAllowed);
      if (!active.current) return;
      loadedMore.current = true;
      replaceRows((previous) => {
        const ids = new Set(previous.map((row) => row.id));
        return [...previous, ...page.items.filter((row) => !ids.has(row.id))];
      });
      if (page.next_cursor === cursor) throw new Error("History returned the same page cursor. Please retry.");
      setCursor(page.next_cursor);
    } catch (failure) {
      if (active.current) setPageError(displayableError(failure));
    } finally {
      pageInFlight.current = false;
      if (active.current) setLoadingMore(false);
    }
  }, [cursor, includeAutoAllowed, replaceRows]);
  useEffect(() => {
    const node = sentinel.current;
    if (!node || !cursor || loading || loadingMore || pageError) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) void loadMore();
      },
      { root: node.closest(".agentplane-shell-main"), rootMargin: "400px" }
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [cursor, loading, loadingMore, pageError, loadMore]);
  const executors = useExecutorKinds();
  const decided = requests.filter((request) => request.state !== "decision_pending");

  return (
    <Stack>
      {error && <Text c="red">{error}</Text>}
      {executors.error && <Alert color="orange">Action group metadata could not be loaded. {executors.error}</Alert>}
      {loading && <Text role="status">Loading actions…</Text>}
      {!loading && !error && decided.length === 0 && (
        <Stack gap="xs">
          <Text c="dimmed">
            {includeAutoAllowed ? "No decided actions yet." : "No actions that weren’t auto-allowed yet."}
          </Text>
          {!includeAutoAllowed && (
            <Button variant="subtle" onClick={showAll}>
              Show all actions
            </Button>
          )}
        </Stack>
      )}
      {decided.map((request) => (
        <ActionHistoryCard
          key={request.id}
          compact
          request={request}
          mcp={executors.kinds?.get(request.action.group) === "mcp"}
        />
      ))}
      <div ref={sentinel} data-testid="action-history-sentinel">
        {loadingMore && <Text role="status">Loading older actions…</Text>}
        {pageError && (
          <Alert color="red" title="Older actions could not be loaded">
            <Text size="sm">{pageError}</Text>
            <Button onClick={() => void loadMore()}>Retry loading older actions</Button>
          </Alert>
        )}
        {!loading && !cursor && decided.length > 0 && (
          <Text c="dimmed" size="sm">
            End of action history.
          </Text>
        )}
      </div>
    </Stack>
  );
}

export function ActionHistory({ embedded = false }: { embedded?: boolean }): JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const filter = searchParams.get("history_filter") === "all" ? "all" : "not-auto-allowed";
  function setFilter(value: string): void {
    setSearchParams((previous) => {
      const next = new URLSearchParams(previous);
      if (value === "all") next.set("history_filter", "all");
      else next.delete("history_filter");
      return next;
    });
  }
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
      {/* Each filter starts at its newest page. Its URL survives navigation and browser Back. */}
      <ActionHistoryRequests key={filter} includeAutoAllowed={filter === "all"} showAll={() => setFilter("all")} />
    </Stack>
  );
}
