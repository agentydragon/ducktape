import { Button, Group, Paper, Stack, Text, Title } from "@mantine/core";
import { type QueryClient, QueryClientProvider, queryOptions } from "@tanstack/react-query";
import { IconCheck, IconX } from "../ui/icons";
import {
  createContext,
  type JSX,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { displayableError } from "../transport/client";
import { useOptionalStreamStatus } from "../transport/stream_status";
import {
  useWatchCollection,
  type CollectionConfig,
  type CollectionFrame,
  type CollectionWatchHandlers,
} from "../transport/watch_collection";
import { createAppQueryClient } from "../transport/query";
import { ActionCall } from "./call";
import {
  ActionState,
  Verdict,
  type ActionGroup,
  type ActionRequest,
  type ActionRequestChunk,
} from "../../../action_service/api/contracts/action_service_pb";
import { timestampText } from "../transport/response_fields";
import { decideActionById, followActionRequests, getAction, listActionGroups } from "./client";

export function actionRequestQuery(requestId: string): ReturnType<typeof queryOptions<ActionRequest>> {
  return queryOptions<ActionRequest>({
    queryKey: ["action-request", requestId],
    queryFn: () => getAction(requestId),
  });
}

export function actionGroupsQuery(): ReturnType<typeof queryOptions<ActionGroup[]>> {
  return queryOptions<ActionGroup[]>({
    queryKey: ["action-groups"],
    queryFn: listActionGroups,
    staleTime: 0,
  });
}

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

const PENDING: CollectionConfig<ActionRequest, ActionRequest> = {
  kind: "ActionRequest",
  key: (request) => request.id,
  // A request that left DECISION_PENDING arrives as an upsert of its new state, which removes what it supersedes.
  removal: { key: (request) => request.id, matches: (held, request) => request.version >= held.version },
  // A decision's HTTP answer and the watch both write a request; keep the newer.
  supersedes: (held, incoming) => incoming.version >= held.version,
};

const pendingChunk = (chunk: ActionRequestChunk): ActionRequest[] => chunk.actionRequests;

function newestFirst(left: ActionRequest, right: ActionRequest): number {
  return Date.parse(timestampText(right.createdAt) ?? "") - Date.parse(timestampText(left.createdAt) ?? "");
}

/** Shared fetch/decide plumbing for the pending and history views, over Action Service watches. */
export function useActionRequests(enabled = true): {
  requests: ActionRequest[];
  error: string | null;
  loading: boolean;
  deciding: string | null;
  decide: (request: ActionRequest, verdict: Verdict) => void;
  queryClient: QueryClient;
} {
  const [error, setError] = useState<string | null>(null);
  const [deciding, setDeciding] = useState<string | null>(null);
  const [queryClient] = useState(() => {
    const client = createAppQueryClient();
    client.setQueryDefaults(["action-request"], {
      // Fence both HTTP and watch writes; other resources have no ActionRequest version.
      structuralSharing: (previous, incoming) => {
        const current = previous as ActionRequest | undefined;
        const next = incoming as ActionRequest;
        return current !== undefined &&
          (current.version > next.version || (current.version === next.version && current.state === next.state))
          ? current
          : next;
      },
    });
    return client;
  });

  useEffect(() => () => queryClient.clear(), [queryClient]);

  const rememberRequests = useCallback(
    (rows: readonly ActionRequest[]): void => {
      for (const request of rows) {
        queryClient.setQueryData(actionRequestQuery(request.id).queryKey, request);
      }
    },
    [queryClient]
  );

  const follow = useCallback(
    (handlers: CollectionWatchHandlers<CollectionFrame<ActionRequest, ActionRequestChunk, ActionRequest>>) =>
      followActionRequests({
        ...handlers,
        onFrame: ({ frame }) => {
          handlers.onFrame(
            frame.case === "upsert" && frame.value.state !== ActionState.DECISION_PENDING
              ? { case: "removed", value: frame.value }
              : frame
          );
          if (frame.case === "upsert") rememberRequests([frame.value]);
          if (frame.case === "snapshotComplete" || frame.case === "upsert") setError(null);
        },
      }),
    [rememberRequests]
  );
  const watch = useWatchCollection(PENDING, enabled ? follow : null, pendingChunk);
  useOptionalStreamStatus("Actions", enabled ? watch.connection : null);
  const requests = useMemo(() => [...(watch.items ?? [])].sort(newestFirst), [watch.items]);

  // A pending request missing from a new snapshot was decided elsewhere: refetch its cached detail.
  const shown = useRef<readonly ActionRequest[]>([]);
  useEffect(() => {
    const ids = new Set(requests.map((request) => request.id));
    for (const previous of shown.current) {
      const queryKey = actionRequestQuery(previous.id).queryKey;
      if (
        previous.state === ActionState.DECISION_PENDING &&
        !ids.has(previous.id) &&
        queryClient.getQueryData(queryKey)?.state === ActionState.DECISION_PENDING
      ) {
        void queryClient.invalidateQueries({ queryKey, exact: true }, { cancelRefetch: false });
      }
    }
    shown.current = requests;
    rememberRequests(requests);
  }, [queryClient, rememberRequests, requests]);

  async function decideRequest(request: ActionRequest, verdict: Verdict): Promise<void> {
    setDeciding(request.id);
    try {
      const updated = await decideActionById(request.id, request.version, verdict);
      watch.replace(updated);
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
    loading: watch.items === null,
    deciding,
    decide: (request, verdict) => void decideRequest(request, verdict),
    queryClient,
  };
}

export const ActionRequestsContext: ReturnType<typeof createContext<ReturnType<typeof useActionRequests> | null>> =
  createContext<ReturnType<typeof useActionRequests> | null>(null);

/** Own one live Action store for the whole app shell, including route transitions. */
export function ActionRequestsProvider({ children }: { children: ReactNode }): JSX.Element {
  const actions = useActionRequests();
  return (
    <QueryClientProvider client={actions.queryClient}>
      <ActionRequestsContext.Provider value={actions}>{children}</ActionRequestsContext.Provider>
    </QueryClientProvider>
  );
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
export function ActionRequests(): JSX.Element {
  const shared = useContext(ActionRequestsContext);
  const local = useActionRequests(shared === null);
  const { requests, error, loading, deciding, decide } = shared ?? local;
  const pending = requests.filter((request) => request.state === ActionState.DECISION_PENDING);

  return (
    <Stack>
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
