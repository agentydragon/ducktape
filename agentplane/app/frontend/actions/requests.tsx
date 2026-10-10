import { Button, Group, Paper, Stack, Text, Title } from "@mantine/core";
import IconCheck from "@tabler/icons-react/dist/esm/icons/IconCheck.mjs";
import IconX from "@tabler/icons-react/dist/esm/icons/IconX.mjs";
import { createContext, type JSX, type ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";

import { displayableError } from "../client";
import { followStream, type StreamConnection } from "../live_stream";
import { StaleNotice, useOptionalStreamStatus, type StreamStatus } from "../stream_status";
import { ActionCall } from "./call";
import { actionService, type ActionRequestView, type ActionService, type ActionState, type Verdict } from "./client";

export function stateLabel(state: ActionState): string {
  return state.replaceAll("_", " ");
}

/** Paired human decisions, shared by the Actions list, detail page, and sidebar quick review. */
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
        onClick={() => onDecide("deny")}
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
        onClick={() => onDecide("allow")}
      >
        Approve
      </Button>
    </Group>
  );
}

/** Shared fetch/decide plumbing for the pending and history views: one live snapshot (the real
 * service pushes over `/actions/stream`) or one polled `list()` (any other service, e.g. tests),
 * which has no `stream`. */
export function useActionRequests(
  service: ActionService,
  enabled = true
): {
  requests: ActionRequestView[];
  error: string | null;
  loading: boolean;
  stream: StreamStatus | null;
  deciding: string | null;
  decide: (request: ActionRequestView, verdict: Verdict) => void;
  knownRequests: ReadonlyMap<string, ActionRequestView>;
  staleRequestIds: ReadonlySet<string>;
  detailLoadingIds: ReadonlySet<string>;
  detailErrors: ReadonlyMap<string, string>;
  loadDetail: (requestId: string, forceRefresh?: boolean) => Promise<ActionRequestView>;
} {
  const [requests, setRequests] = useState<ActionRequestView[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [connection, setConnection] = useState<StreamConnection | null>(null);
  const [deciding, setDeciding] = useState<string | null>(null);
  const [knownRequests, setKnownRequests] = useState<ReadonlyMap<string, ActionRequestView>>(() => new Map());
  const [staleRequestIds, setStaleRequestIds] = useState<ReadonlySet<string>>(() => new Set());
  const [detailLoadingIds, setDetailLoadingIds] = useState<ReadonlySet<string>>(() => new Set());
  const [detailErrors, setDetailErrors] = useState<ReadonlyMap<string, string>>(() => new Map());
  const requestsRef = useRef<ActionRequestView[]>([]);
  const knownRequestsRef = useRef(new Map<string, ActionRequestView>());
  const staleRequestIdsRef = useRef(new Set<string>());
  const detailLoadsRef = useRef(new Map<string, Promise<ActionRequestView>>());
  const stream = useOptionalStreamStatus("Actions", connection);

  const rememberRequests = useCallback((rows: readonly ActionRequestView[]): void => {
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
    (rows: ActionRequestView[]): void => {
      const nextIds = new Set(rows.map((request) => request.id));
      const stale = new Set(staleRequestIdsRef.current);
      for (const previous of requestsRef.current) {
        if (
          previous.state === "decision_pending" &&
          !nextIds.has(previous.id) &&
          knownRequestsRef.current.get(previous.id)?.state === "decision_pending"
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

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      storePendingSnapshot(await service.list());
      setError(null);
    } catch (failure) {
      setError(displayableError(failure));
    } finally {
      setLoading(false);
    }
  }, [service, storePendingSnapshot]);

  const loadDetail = useCallback(
    (requestId: string, forceRefresh = false): Promise<ActionRequestView> => {
      const cached = knownRequestsRef.current.get(requestId);
      if (!forceRefresh && !staleRequestIdsRef.current.has(requestId) && cached !== undefined) {
        return Promise.resolve(cached);
      }
      const inFlight = detailLoadsRef.current.get(requestId);
      if (inFlight !== undefined) return inFlight;
      const get = service.get;
      if (get === undefined) {
        const failure = new Error("Action detail loading is unavailable.");
        setDetailErrors((current) => new Map(current).set(requestId, displayableError(failure)));
        return Promise.reject(failure);
      }

      setDetailLoadingIds((current) => new Set(current).add(requestId));
      setDetailErrors((current) => {
        if (!current.has(requestId)) return current;
        const next = new Map(current);
        next.delete(requestId);
        return next;
      });
      const request = Promise.resolve()
        .then(() => get.call(service, requestId))
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
    [rememberRequests, service]
  );

  useEffect(() => {
    if (!enabled) return;
    if (service !== actionService) {
      void refresh();
      return;
    }
    return followStream("/actions/stream?state=decision_pending", {
      events: {
        snapshot: (message) => {
          try {
            storePendingSnapshot(JSON.parse(message.data) as ActionRequestView[]);
            setError(null);
          } catch {
            setError("The live Action update was invalid.");
          } finally {
            setLoading(false);
          }
        },
      },
      onConnection: setConnection,
    });
  }, [enabled, refresh, service, storePendingSnapshot]);

  async function decideRequest(request: ActionRequestView, verdict: Verdict): Promise<void> {
    setDeciding(request.id);
    try {
      const updated = await service.decide(request, verdict);
      const current = requestsRef.current;
      const next = current.map((item) => (item.id === updated.id && updated.version >= item.version ? updated : item));
      requestsRef.current = next;
      setRequests(next);
      rememberRequests([updated]);
      setError(null);
      if (service !== actionService) await refresh();
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
    stream,
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
  const actions = useActionRequests(actionService);
  return <ActionRequestsContext.Provider value={actions}>{children}</ActionRequestsContext.Provider>;
}

export function PendingActionCard({
  request,
  deciding,
  onDecide,
}: {
  request: ActionRequestView;
  deciding: boolean;
  onDecide: (request: ActionRequestView, verdict: Verdict) => void;
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
export function ActionRequests({
  service = actionService,
  embedded = false,
}: {
  service?: ActionService;
  embedded?: boolean;
}): JSX.Element {
  const shared = useContext(ActionRequestsContext);
  const local = useActionRequests(service, !(shared !== null && service === actionService));
  const { requests, error, loading, stream, deciding, decide } =
    shared !== null && service === actionService ? shared : local;
  const pending = requests.filter((request) => request.state === "decision_pending");

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
      <StaleNotice streams={[stream]} />
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
