import { Button, Group, Paper, Stack, Text, Title } from "@mantine/core";
import { IconCheck, IconX } from "../icons";
import { createContext, type JSX, type ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";

import { displayableError } from "../client";
import type { StreamConnection } from "../stream_reader";
import { useOptionalStreamStatus } from "../stream_status";
import { ActionCall } from "./call";
import { ActionState, Verdict, type ActionRequest } from "../../../action_service/action_service_pb";
import { timestampText } from "./presentation";
import { decideAction, followActionRequests, getAction } from "./client";

export function stateLabel(state: ActionState): string {
  switch (state) {
    case ActionState.DECISION_PENDING:
      return "decision pending";
    case ActionState.ALLOWED:
      return "allowed";
    case ActionState.DENIED:
      return "denied";
    case ActionState.DISPATCHING:
      return "dispatching";
    case ActionState.RUNNING:
      return "running";
    case ActionState.SUCCEEDED:
      return "succeeded";
    case ActionState.FAILED:
      return "failed";
    case ActionState.CANCELLED:
      return "cancelled";
    case ActionState.EXECUTION_UNKNOWN:
      return "execution unknown";
    default:
      throw new Error(`Unknown Action state (${state}).`);
  }
}

/** Paired human decisions, shared by the Actions list, detail page, and compact card. */
export function ActionDecisionButtons({
  deciding,
  onDecide,
  size,
  title,
}: {
  deciding: boolean;
  onDecide: (verdict: Verdict) => void;
  size: "xs" | "sm";
  title?: string;
}): JSX.Element {
  const iconSize = size === "sm" ? 15 : 13;
  return (
    <Group gap={6} wrap="nowrap">
      <Button
        className={size === "sm" ? "action-decision-button" : undefined}
        aria-label={title === undefined ? "Deny" : `Deny ${title}`}
        size={size}
        variant="light"
        color="red"
        leftSection={<IconX size={iconSize} aria-hidden="true" />}
        loading={deciding}
        onClick={() => onDecide(Verdict.DENY)}
      >
        Deny
      </Button>
      <Button
        className={size === "sm" ? "action-decision-button" : undefined}
        aria-label={title === undefined ? "Approve" : `Approve ${title}`}
        size={size}
        variant="filled"
        color="green"
        leftSection={<IconCheck size={iconSize} aria-hidden="true" />}
        loading={deciding}
        onClick={() => onDecide(Verdict.ALLOW)}
      >
        Approve
      </Button>
    </Group>
  );
}

/** Shared fetch/decide plumbing for the pending and history views, over Action Service watches. */
export function useActionRequests(enabled = true): {
  requests: ActionRequest[];
  error: string | null;
  loading: boolean;
  deciding: string | null;
  decide: (request: ActionRequest, verdict: Verdict) => void;
  knownRequests: ReadonlyMap<string, ActionRequest>;
  staleRequestIds: ReadonlySet<string>;
  detailLoadingIds: ReadonlySet<string>;
  detailErrors: ReadonlyMap<string, string>;
  loadDetail: (requestId: string, forceRefresh?: boolean) => Promise<ActionRequest>;
} {
  const [requests, setRequests] = useState<ActionRequest[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [connection, setConnection] = useState<StreamConnection | null>(null);
  const [deciding, setDeciding] = useState<string | null>(null);
  const [knownRequests, setKnownRequests] = useState<ReadonlyMap<string, ActionRequest>>(() => new Map());
  const [staleRequestIds, setStaleRequestIds] = useState<ReadonlySet<string>>(() => new Set());
  const [detailLoadingIds, setDetailLoadingIds] = useState<ReadonlySet<string>>(() => new Set());
  const [detailErrors, setDetailErrors] = useState<ReadonlyMap<string, string>>(() => new Map());
  const requestsRef = useRef<ActionRequest[]>([]);
  const knownRequestsRef = useRef(new Map<string, ActionRequest>());
  const staleRequestIdsRef = useRef(new Set<string>());
  const detailLoadsRef = useRef(new Map<string, Promise<ActionRequest>>());
  useOptionalStreamStatus("Actions", connection);

  const rememberRequests = useCallback((rows: readonly ActionRequest[]): void => {
    let next = knownRequestsRef.current;
    let changed = false;
    for (const request of rows) {
      const previous = next.get(request.id);
      if (
        previous !== undefined &&
        (previous.version > request.version ||
          (previous.version === request.version && previous.state === request.state))
      ) {
        continue;
      }
      if (!changed) next = new Map(next);
      next.set(request.id, request);
      changed = true;
    }
    if (changed) {
      knownRequestsRef.current = next;
      setKnownRequests(next);
    }

    const freshIds = rows.filter((request) => staleRequestIdsRef.current.has(request.id)).map((request) => request.id);
    if (freshIds.length > 0) {
      const fresh = new Set(staleRequestIdsRef.current);
      for (const requestId of freshIds) fresh.delete(requestId);
      staleRequestIdsRef.current = fresh;
      setStaleRequestIds(fresh);
    }

    setDetailErrors((current) => {
      if (!rows.some((request) => current.has(request.id))) return current;
      const next = new Map(current);
      for (const request of rows) {
        next.delete(request.id);
      }
      return next;
    });
  }, []);

  const storePendingSnapshot = useCallback(
    (rows: ActionRequest[]): void => {
      const nextIds = new Set(rows.map((request) => request.id));
      const stale = new Set(staleRequestIdsRef.current);
      for (const previous of requestsRef.current) {
        if (
          previous.state === ActionState.DECISION_PENDING &&
          !nextIds.has(previous.id) &&
          knownRequestsRef.current.get(previous.id)?.state === ActionState.DECISION_PENDING
        ) {
          stale.add(previous.id);
        }
      }
      for (const requestId of nextIds) stale.delete(requestId);
      if (
        stale.size !== staleRequestIdsRef.current.size ||
        [...stale].some((id) => !staleRequestIdsRef.current.has(id))
      ) {
        staleRequestIdsRef.current = stale;
        setStaleRequestIds(stale);
      }
      requestsRef.current = rows;
      setRequests(rows);
      rememberRequests(rows);
    },
    [rememberRequests]
  );

  const loadDetail = useCallback(
    (requestId: string, forceRefresh = false): Promise<ActionRequest> => {
      const cached = knownRequestsRef.current.get(requestId);
      if (!forceRefresh && !staleRequestIdsRef.current.has(requestId) && cached !== undefined) {
        return Promise.resolve(cached);
      }
      const inFlight = detailLoadsRef.current.get(requestId);
      if (inFlight !== undefined) return inFlight;
      setDetailLoadingIds((current) => new Set(current).add(requestId));
      setDetailErrors((current) => {
        if (!current.has(requestId)) return current;
        const next = new Map(current);
        next.delete(requestId);
        return next;
      });
      const request = Promise.resolve()
        .then(() => getAction(requestId))
        .then((loaded) => {
          rememberRequests([loaded]);
          return loaded;
        })
        .catch((failure: unknown) => {
          setDetailErrors((current) => new Map(current).set(requestId, displayableError(failure)));
          throw failure;
        })
        .finally(() => {
          detailLoadsRef.current.delete(requestId);
          setDetailLoadingIds((current) => {
            if (!current.has(requestId)) return current;
            const next = new Set(current);
            next.delete(requestId);
            return next;
          });
        });
      detailLoadsRef.current.set(requestId, request);
      return request;
    },
    [rememberRequests]
  );

  useEffect(() => {
    if (!enabled) return;
    let snapshot: ActionRequest[] = [];
    return followActionRequests({
      onOpen: () => {
        snapshot = [];
      },
      onFrame: (frame) => {
        switch (frame.kind) {
          case "snapshot":
            snapshot.push(...frame.requests);
            break;
          case "snapshot_complete":
            storePendingSnapshot(snapshot);
            snapshot = [];
            setError(null);
            setLoading(false);
            break;
          case "upsert": {
            const current = requestsRef.current;
            const exists = current.some((item) => item.id === frame.request.id);
            const next =
              frame.request.state === ActionState.DECISION_PENDING
                ? exists
                  ? current.map((item) =>
                      item.id === frame.request.id && frame.request.version >= item.version ? frame.request : item
                    )
                  : [...current, frame.request].sort(
                      (left, right) =>
                        Date.parse(timestampText(right.createdAt) ?? "") -
                        Date.parse(timestampText(left.createdAt) ?? "")
                    )
                : current.filter((item) => item.id !== frame.request.id);
            requestsRef.current = next;
            setRequests(next);
            rememberRequests([frame.request]);
            setError(null);
            setLoading(false);
            break;
          }
          case "reconnect_required":
            break;
        }
      },
      onConnection: setConnection,
    });
  }, [enabled, rememberRequests, storePendingSnapshot]);

  async function decideRequest(request: ActionRequest, verdict: Verdict): Promise<void> {
    setDeciding(request.id);
    try {
      const updated = await decideAction(request, verdict);
      const current = requestsRef.current;
      const next = current.map((item) => (item.id === updated.id && updated.version >= item.version ? updated : item));
      requestsRef.current = next;
      setRequests(next);
      rememberRequests([updated]);
      setError(null);
    } catch (failure) {
      setError(displayableError(failure));
    } finally {
      setDeciding(null);
    }
  }

  return {
    requests,
    error,
    loading,
    deciding,
    decide: (request, verdict) => void decideRequest(request, verdict),
    knownRequests,
    staleRequestIds,
    detailLoadingIds,
    detailErrors,
    loadDetail,
  };
}

export const ActionRequestsContext: ReturnType<typeof createContext<ReturnType<typeof useActionRequests> | null>> =
  createContext<ReturnType<typeof useActionRequests> | null>(null);

/** Own one live Action store for the whole app shell, including route transitions. */
export function ActionRequestsProvider({ children }: { children: ReactNode }): JSX.Element {
  const actions = useActionRequests();
  return <ActionRequestsContext.Provider value={actions}>{children}</ActionRequestsContext.Provider>;
}

export function PendingActionCard({
  request,
  deciding,
  onDecide,
}: {
  request: ActionRequest;
  deciding: boolean;
  onDecide: (request: ActionRequest, verdict: Verdict) => void;
}): JSX.Element {
  const [raw, setRaw] = useState(false);
  return (
    <Paper withBorder p="md">
      <Stack gap="sm">
        <ActionCall
          request={request}
          headerActions={
            <ActionDecisionButtons size="sm" deciding={deciding} onDecide={(verdict) => onDecide(request, verdict)} />
          }
          raw={raw}
          onRawChange={setRaw}
          prettyResult={false}
        />
      </Stack>
    </Paper>
  );
}

/** The primary, actionable view: ActionRequests still awaiting an operator decision. Decided and
 * terminal requests follow these in the unified Actions view. */
export function ActionRequests({ embedded = false }: { embedded?: boolean }): JSX.Element {
  const shared = useContext(ActionRequestsContext);
  const local = useActionRequests(shared === null);
  const { requests, error, loading, deciding, decide } = shared ?? local;
  const pending = requests.filter((request) => request.state === ActionState.DECISION_PENDING);

  return (
    <Stack>
      {!embedded && (
        <div>
          <Title order={2}>Actions</Title>
          <Text c="dimmed" size="sm">
            Review pending ActionRequests. Allow dispatches the single permitted Execution automatically.
          </Text>
        </div>
      )}
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
