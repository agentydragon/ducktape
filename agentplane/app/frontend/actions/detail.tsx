import { Alert, Button, Group, Stack, Text, Title } from "@mantine/core";
import IconArrowLeft from "@tabler/icons-react/dist/esm/icons/IconArrowLeft.mjs";
import { type JSX, useContext, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router";

import { displayableError } from "../client";
import { ActionRequestsContext, PendingActionCard } from "./requests";
import { ActionHistoryCard } from "./history";
import { actionGroupService, actionService, type ActionRequestView, type ActionService } from "./client";

function hasInAppReturnTo(value: unknown): boolean {
  if (typeof value === "string") return value.startsWith("/") && !value.startsWith("//");
  if (typeof value !== "object" || value === null || !("pathname" in value)) return false;
  const pathname = (value as { pathname?: unknown }).pathname;
  return typeof pathname === "string" && pathname.startsWith("/") && !pathname.startsWith("//");
}

/** Full review page for one pending request or its durable terminal receipt. */
export function ActionRequestDetail({
  requestId,
  service = actionService,
}: {
  requestId: string;
  service?: ActionService;
}): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const actions = useContext(ActionRequestsContext);
  const [loadedRequest, setLoadedRequest] = useState<ActionRequestView | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [executorKind, setExecutorKind] = useState<string | null>(null);
  const wasInPendingStream = useRef(false);
  const historyState = location.state as { returnTo?: unknown } | null;
  const hasReturnTo = hasInAppReturnTo(historyState?.returnTo);
  const liveRequest = actions?.requests.find((item) => item.id === requestId);
  const request = liveRequest ?? loadedRequest;

  useEffect(() => {
    let active = true;
    setLoadedRequest(null);
    setLoading(true);
    setError(null);
    const get = service.get;
    if (get === undefined) {
      setError("Action detail loading is unavailable.");
      setLoading(false);
      return () => {
        active = false;
      };
    }
    void get
      .call(service, requestId)
      .then(
        (loaded) => {
          if (active) setLoadedRequest(loaded);
        },
        (failure: unknown) => {
          if (active) setError(displayableError(failure));
        }
      )
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [requestId, service]);

  useEffect(() => {
    if (liveRequest?.state === "decision_pending") wasInPendingStream.current = true;
  }, [liveRequest?.id, liveRequest?.state]);

  useEffect(() => {
    // The live stream contains pending requests only. If a request we saw there disappears, fetch
    // its durable receipt so this detail view reflects a decision made in another tab/operator.
    if (
      !wasInPendingStream.current ||
      actions?.loading !== false ||
      liveRequest !== undefined ||
      loadedRequest?.state !== "decision_pending"
    ) {
      return;
    }
    const get = service.get;
    if (get === undefined) return;
    let active = true;
    setLoading(true);
    setError(null);
    void get
      .call(service, requestId)
      .then(
        (loaded) => {
          if (active) setLoadedRequest(loaded);
        },
        (failure: unknown) => {
          if (active) setError(displayableError(failure));
        }
      )
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [actions?.loading, liveRequest, loadedRequest?.state, loadedRequest?.version, requestId, service]);

  useEffect(() => {
    if (request === null || request.state === "decision_pending") {
      setExecutorKind(null);
      return;
    }
    let active = true;
    void actionGroupService.list().then(
      (groups) => {
        if (active) setExecutorKind(groups.find((group) => group.key === request.action.group)?.executor_kind ?? null);
      },
      () => {
        if (active) setExecutorKind(null);
      }
    );
    return () => {
      active = false;
    };
  }, [request?.id, request?.state, request?.action.group]);

  function goBack(): void {
    if (hasReturnTo) {
      void navigate(-1);
    } else {
      void navigate("/actions", { replace: true });
    }
  }

  return (
    <Stack>
      <Group align="center" gap="sm">
        <Button variant="subtle" size="sm" leftSection={<IconArrowLeft size={15} />} onClick={goBack}>
          Back
        </Button>
        <Title order={1} size="h4">
          Action details
        </Title>
      </Group>
      {error !== null && (
        <Alert color="red" title="Could not load this action">
          {error}
        </Alert>
      )}
      {loading && <Text role="status">Loading action details…</Text>}
      {!loading && error === null && request === null && <Text c="dimmed">This action is no longer available.</Text>}
      {!loading && error === null && request?.state === "decision_pending" && (
        <Stack>
          {actions?.error !== null && actions?.error !== undefined && (
            <Alert color="red" title="Action update failed">
              {actions.error}
            </Alert>
          )}
          <PendingActionCard
            request={request}
            deciding={actions?.deciding === request.id}
            onDecide={(row, verdict) => actions?.decide(row, verdict)}
          />
        </Stack>
      )}
      {!loading && error === null && request !== null && request.state !== "decision_pending" && (
        <ActionHistoryCard request={request} mcp={executorKind === "mcp"} />
      )}
    </Stack>
  );
}
