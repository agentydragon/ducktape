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
import { useQuery } from "@tanstack/react-query";

import { Disclosure, DisclosureBody } from "../../../../util/frontend/disclosure";
import { durationLabel } from "../format_time";
import { IconArrowRight } from "../icons";
import { RawSwitch } from "../raw_switch";
import { TimeAgo } from "../time_ago";
import { displayableError } from "../client";
import { ClampedBlock, lineCount, TEXT_MAX_HEIGHT_REM } from "../../../../util/frontend/clamped_block";
import { JsonBlock } from "../text_block";
import { useOptionalStreamStatus } from "../stream_status";
import type { StreamConnection } from "../stream_reader";
import { ActionCall } from "./call";
import { CallToolResultView, parseCallToolResult, type CallToolResult } from "./call_tool_result";
import {
  ActionState,
  ExecutorKind,
  Verdict,
  type ActionRequest,
  type Decision,
} from "../../../action_service/action_service_pb";
import {
  actionArguments,
  actionIdentity,
  executionError as projectExecutionError,
  executionResult,
  timestampText,
} from "./presentation";
import { actionHistory, followActionRequests, getAction } from "./client";
import {
  renderActionCall,
  renderActionOutcome,
  renderActionLabel,
  renderDetailsArguments,
  renderDetailsResult,
  renderNativeDetailsResult,
} from "./rendering/index";
import { actionGroupsQuery, stateLabel } from "./requests";

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
  decision: Decision;
  requestedAt: string;
  named: boolean;
}): JSX.Element {
  const verb = named
    ? "Decided"
    : decision.verdict === Verdict.DENY
      ? "Denied"
      : decision.operator === undefined
        ? "Auto-approved"
        : "Approved";
  if (decision.operator === undefined) {
    const policySet = decision.policyEvidence?.matched?.policySet;
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
  const decidedAt = timestampText(decision.decidedAt);
  if (decidedAt === undefined) throw new Error("The Action Service omitted Decision.decided_at.");
  const wait = durationLabel(Math.round((Date.parse(decidedAt) - Date.parse(requestedAt)) / 1000));
  return <>{wait === "" ? verb : `${verb} after ${wait}`}</>;
}

/** A decided/terminal ActionRequest, kept as a durable receipt: the call as the operator saw it
 * when deciding, then the decision it is a record of and what came of it. */
export function ActionHistoryCard({
  request,
  mcp,
  compact = false,
}: {
  request: ActionRequest;
  mcp: boolean;
  compact?: boolean;
}): JSX.Element {
  const location = useLocation();
  const decision = request.decision;
  const action = actionIdentity(request);
  const args = actionArguments(request);
  const [raw, setRaw] = useState(false);
  const result = executionResult(request.execution);
  const executionError = projectExecutionError(request.execution);
  const hasResult = result !== undefined && result !== null;
  const hasExecutionError = executionError !== undefined;
  const decisionState = request.state === ActionState.ALLOWED || request.state === ActionState.DENIED;
  const call = mcp && hasResult ? parseCallToolResult(result) : null;
  const prettyResult = mcp
    ? call === null
      ? undefined
      : renderDetailsResult(action, call)
    : hasResult
      ? renderNativeDetailsResult(action, result)
      : undefined;
  const combinedCall = renderActionCall(action, args, call ?? undefined);
  const actionOutcome = call === null ? undefined : renderActionOutcome(action, call);
  const toolError = request.state === ActionState.SUCCEEDED && call?.isError === true;
  const decisionLabel =
    decision === undefined
      ? undefined
      : decision.verdict === Verdict.ALLOW
        ? decision.operator === undefined
          ? "Auto-allowed"
          : "Allowed"
        : "Denied";
  const outcomeLabel = toolError
    ? "Tool error"
    : (actionOutcome?.label ??
      (decisionState ? (decisionLabel ?? stateLabel(request.state)) : stateLabel(request.state)));
  const failure =
    [ActionState.FAILED, ActionState.CANCELLED, ActionState.EXECUTION_UNKNOWN].includes(request.state) ||
    toolError ||
    actionOutcome?.failed === true;
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
    : request.state === ActionState.SUCCEEDED
      ? "green"
      : request.state === ActionState.DENIED
        ? "gray"
        : "blue";
  const status = (
    <Badge color={color} data-testid="action-execution-state">
      {outcomeLabel}
    </Badge>
  );
  const timestamp = (
    <Text size="xs" c="dimmed">
      {decision !== undefined && (
        <>
          <DecisionSummary
            decision={decision}
            requestedAt={timestampText(request.createdAt) ?? ""}
            named={decisionState}
          />
          {" · "}
        </>
      )}
      {request.execution?.completedAt ? "Completed " : "Updated "}
      <TimeAgo value={timestampText(request.execution?.completedAt ?? request.updatedAt) ?? ""} />
    </Text>
  );
  const pretty =
    call !== null ||
    prettyResult !== undefined ||
    renderDetailsArguments(action, args) !== undefined ||
    combinedCall !== undefined;
  const actionLabel = renderActionLabel(action, args);
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
      {decision?.decisionNote && (
        <ClampedBlock maxHeightRem={TEXT_MAX_HEIGHT_REM} lines={lineCount(decision.decisionNote)} label="Decision note">
          <Text size="sm">{decision.decisionNote}</Text>
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
                    {action.group} / {action.name}
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

/** Decided and terminal ActionRequests: durable receipts, not something an operator still acts on. */
function ActionHistoryRequests({
  includeAutoAllowed,
  showAll,
}: {
  includeAutoAllowed: boolean;
  showAll: () => void;
}): JSX.Element {
  const [requests, setRequests] = useState<ActionRequest[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [pageError, setPageError] = useState<string | null>(null);
  const rows = useRef<ActionRequest[]>([]);
  const loadedMore = useRef(false);
  const pageInFlight = useRef(false);
  const active = useRef(true);
  const sentinel = useRef<HTMLDivElement>(null);
  const [connection, setConnection] = useState<StreamConnection | null>(null);
  useOptionalStreamStatus("Actions", connection);
  const replaceRows = useCallback((update: (previous: ActionRequest[]) => ActionRequest[]): void => {
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
        const pageIds = new Set(page.actionRequests.map((row) => row.id));
        const unfinished = rows.current.filter(
          (row) =>
            !pageIds.has(row.id) &&
            [ActionState.ALLOWED, ActionState.DISPATCHING, ActionState.RUNNING].includes(row.state)
        );
        const updates = await Promise.all(unfinished.map((row) => getAction(row.id)));
        if (!active.current || current !== generation) return;
        replaceRows((previous) => {
          const fresh = new Map([...updates, ...page.actionRequests].map((row) => [row.id, row]));
          // A page response can race with a newer receipt already seen on another request.
          for (const row of previous) {
            const candidate = fresh.get(row.id);
            if (candidate && row.version > candidate.version) fresh.set(row.id, row);
          }
          const latest = page.actionRequests.map((row) => fresh.get(row.id)!);
          if (!loadedMore.current) return latest;
          const firstPageIds = new Set(page.actionRequests.map((row) => row.id));
          const older = previous.filter((row) => !firstPageIds.has(row.id));
          return [...latest, ...older.map((row) => fresh.get(row.id) ?? row)];
        });
        if (!loadedMore.current) setCursor(page.nextCursor ?? null);
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
        return [...previous, ...page.actionRequests.filter((row) => !ids.has(row.id))];
      });
      if (page.nextCursor === cursor) throw new Error("History returned the same page cursor. Please retry.");
      setCursor(page.nextCursor ?? null);
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
  const groups = useQuery(actionGroupsQuery());
  const executors = new Map(groups.data?.map((group) => [group.key, group.executorKind]));
  const decided = requests.filter((request) => request.state !== ActionState.DECISION_PENDING);

  return (
    <Stack>
      {error && <Text c="red">{error}</Text>}
      {groups.error && (
        <Alert color="orange">Action group metadata could not be loaded. {displayableError(groups.error)}</Alert>
      )}
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
          mcp={executors.get(actionIdentity(request).group) === ExecutorKind.MCP}
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
