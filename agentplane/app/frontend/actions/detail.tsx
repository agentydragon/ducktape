import { Alert, Button, Group, Stack, Text, Title } from "@mantine/core";
import IconArrowLeft from "@tabler/icons-react/dist/esm/icons/IconArrowLeft.mjs";
import { type JSX, useContext, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router";

import { ActionRequestsContext, PendingActionCard } from "./requests";
import { ActionHistoryCard } from "./history";
import { actionGroupService } from "./client";

function hasInAppReturnTo(value: unknown): boolean {
  if (typeof value === "string") return value.startsWith("/") && !value.startsWith("//");
  if (typeof value !== "object" || value === null || !("pathname" in value)) return false;
  const pathname = (value as { pathname?: unknown }).pathname;
  return typeof pathname === "string" && pathname.startsWith("/") && !pathname.startsWith("//");
}

/** Full review page for one pending request or its durable terminal receipt. */
export function ActionRequestDetail({ requestId }: { requestId: string }): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const actions = useContext(ActionRequestsContext);
  const [executorKind, setExecutorKind] = useState<string | null>(null);
  const historyState = location.state as { returnTo?: unknown } | null;
  const hasReturnTo = hasInAppReturnTo(historyState?.returnTo);
  const liveRequest = actions?.requests.find((item) => item.id === requestId);
  const cachedRequest = actions?.knownRequests.get(requestId);
  const request = liveRequest ?? cachedRequest;
  const detailRequestId = request?.id;
  const detailRequestState = request?.state;
  const detailActionGroup = request?.action.group;
  const stalePendingRequest =
    liveRequest === undefined && cachedRequest?.state === "decision_pending" && actions?.staleRequestIds.has(requestId);
  const loading = actions?.detailLoadingIds.has(requestId) ?? false;
  const error = actions?.detailErrors.get(requestId) ?? null;
  const knownRequests = actions?.knownRequests;
  const staleRequestIds = actions?.staleRequestIds;
  const loadDetail = actions?.loadDetail;

  useEffect(() => {
    if (
      knownRequests === undefined ||
      staleRequestIds === undefined ||
      loadDetail === undefined ||
      liveRequest !== undefined
    ) {
      return;
    }
    const cached = knownRequests.get(requestId);
    const refresh = cached !== undefined && staleRequestIds.has(requestId);
    if (cached !== undefined && !refresh) return;
    void loadDetail(requestId, refresh).catch(() => undefined);
  }, [knownRequests, loadDetail, liveRequest, requestId, staleRequestIds]);

  useEffect(() => {
    if (detailRequestId === undefined || detailRequestState === "decision_pending") {
      setExecutorKind(null);
      return;
    }
    let active = true;
    void actionGroupService.list().then(
      (groups) => {
        if (active) setExecutorKind(groups.find((group) => group.key === detailActionGroup)?.executor_kind ?? null);
      },
      () => {
        if (active) setExecutorKind(null);
      }
    );
    return () => {
      active = false;
    };
  }, [detailActionGroup, detailRequestId, detailRequestState]);

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
      {!loading && !stalePendingRequest && error === null && request === undefined && (
        <Text c="dimmed">This action is no longer available.</Text>
      )}
      {!loading && !stalePendingRequest && error === null && request?.state === "decision_pending" && (
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
      {!loading && error === null && request !== undefined && request.state !== "decision_pending" && (
        <ActionHistoryCard request={request} mcp={executorKind === "mcp"} />
      )}
    </Stack>
  );
}
