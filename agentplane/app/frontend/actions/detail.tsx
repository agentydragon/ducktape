import { Alert, Button, Group, Stack, Text, Title } from "@mantine/core";
import IconArrowLeft from "@tabler/icons-react/dist/esm/icons/IconArrowLeft.mjs";
import { type JSX, useContext, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router";

import { ActionRequestsContext, PendingActionCard } from "./requests";
import { ActionHistoryCard } from "./history";
import { actionGroupService } from "./client";

type InAppReturnTo = string | { pathname: string; search?: string; hash?: string };

function inAppReturnTo(value: unknown): InAppReturnTo | null {
  if (typeof value === "string") {
    return value.startsWith("/") && !value.startsWith("//") ? value : null;
  }
  if (typeof value !== "object" || value === null || !("pathname" in value)) return null;
  const location = value as { pathname?: unknown; search?: unknown; hash?: unknown };
  if (
    typeof location.pathname !== "string" ||
    !location.pathname.startsWith("/") ||
    location.pathname.startsWith("//")
  ) {
    return null;
  }
  return {
    pathname: location.pathname,
    ...(typeof location.search === "string" ? { search: location.search } : {}),
    ...(typeof location.hash === "string" ? { hash: location.hash } : {}),
  };
}

/** Full review page for one pending request or its durable terminal receipt. */
export function ActionRequestDetail({ requestId }: { requestId: string }): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const actions = useContext(ActionRequestsContext);
  const [executorKind, setExecutorKind] = useState<string | null>(null);
  const [returnAfterDecision, setReturnAfterDecision] = useState<{ requestId: string; version: number } | null>(null);
  const historyState = location.state as { returnTo?: unknown } | null;
  const returnTo = inAppReturnTo(historyState?.returnTo);
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
    if (returnAfterDecision === null || returnTo === null) return;
    const decided = actions?.knownRequests.get(returnAfterDecision.requestId);
    if (
      decided === undefined ||
      decided.state === "decision_pending" ||
      decided.version <= returnAfterDecision.version
    ) {
      return;
    }
    void navigate(returnTo, { replace: true });
  }, [actions?.knownRequests, navigate, returnAfterDecision, returnTo]);

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
    if (returnTo !== null) {
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
            onDecide={(row, verdict) => {
              if (actions === null) return;
              setReturnAfterDecision({ requestId: row.id, version: row.version });
              actions.decide(row, verdict);
            }}
          />
        </Stack>
      )}
      {!loading && error === null && request !== undefined && request.state !== "decision_pending" && (
        <ActionHistoryCard request={request} mcp={executorKind === "mcp"} />
      )}
    </Stack>
  );
}
