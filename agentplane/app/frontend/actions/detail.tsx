import { Alert, Button, Group, Stack, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { IconArrowLeft } from "../icons";
import { type JSX, useContext, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router";

import { actionGroupsQuery, actionRequestQuery, ActionRequestsContext, PendingActionCard } from "./requests";
import { displayableError } from "../client";
import { ActionHistoryCard } from "./history";
import { ActionState, ExecutorKind } from "../../../action_service/action_service_pb";
import { actionIdentity } from "./presentation";

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
export function ActionRequestDetail({
  requestId,
  onDone,
}: {
  requestId: string;
  /** A docked pane closes itself, after Back and after a decision, instead of navigating the route. */
  onDone?: () => void;
}): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const actions = useContext(ActionRequestsContext);
  if (actions === null) throw new Error("Action details require an ActionRequestsProvider.");
  const detail = useQuery(actionRequestQuery(requestId), actions.queryClient);
  const [returnAfterDecision, setReturnAfterDecision] = useState<{ requestId: string; version: bigint } | null>(null);
  const historyState = location.state as { returnTo?: unknown } | null;
  const returnTo = inAppReturnTo(historyState?.returnTo);
  const request = detail.data;
  const detailActionGroup = request === undefined ? undefined : actionIdentity(request).group;
  const groups = useQuery({
    ...actionGroupsQuery(),
    enabled: request !== undefined && request.state !== ActionState.DECISION_PENDING,
  });
  const executorKind = groups.data?.find((group) => group.key === detailActionGroup)?.executorKind;
  const stalePendingRequest = request?.state === ActionState.DECISION_PENDING && detail.isStale;
  const loading = detail.isFetching;
  const error = detail.error === null ? null : displayableError(detail.error);

  useEffect(() => {
    if (returnAfterDecision === null || (returnTo === null && onDone === undefined)) return;
    if (
      request === undefined ||
      request.id !== returnAfterDecision.requestId ||
      request.state === ActionState.DECISION_PENDING ||
      request.version <= returnAfterDecision.version
    ) {
      return;
    }
    if (onDone !== undefined) onDone();
    else if (returnTo !== null) void navigate(returnTo, { replace: true });
  }, [request, navigate, onDone, returnAfterDecision, returnTo]);

  function goBack(): void {
    if (onDone !== undefined) {
      onDone();
    } else if (returnTo !== null) {
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
      {!loading && !stalePendingRequest && error === null && request?.state === ActionState.DECISION_PENDING && (
        <Stack>
          {actions.error !== null && (
            <Alert color="red" title="Action update failed">
              {actions.error}
            </Alert>
          )}
          <PendingActionCard
            request={request}
            deciding={actions.deciding === request.id}
            onDecide={(row, verdict) => {
              setReturnAfterDecision({ requestId: row.id, version: row.version });
              actions.decide(row, verdict);
            }}
          />
        </Stack>
      )}
      {!loading && error === null && request !== undefined && request.state !== ActionState.DECISION_PENDING && (
        <ActionHistoryCard request={request} mcp={executorKind === ExecutorKind.MCP} />
      )}
    </Stack>
  );
}
