import { Badge, Button, Group, Paper, Stack, Text } from "@mantine/core";
import IconBell from "@tabler/icons-react/dist/esm/icons/IconBell.mjs";
import { type JSX, type ReactNode, useContext, useEffect, useId, useState } from "react";
import { useNavigate } from "react-router";

import { StaleNotice } from "../stream_status";
import { TopbarActions } from "../topbar";
import { actionService } from "./client";
import { ActionRequestsContext, PendingActionCard, useActionRequests } from "./requests";

/** One stream and decision state for the shell, the Actions page, and the thread composer. */
export function ActionAffordance({ children }: { children: ReactNode }): JSX.Element {
  const actions = useActionRequests(actionService);
  const pending = actions.requests.filter((request) => request.state === "decision_pending");
  const navigate = useNavigate();

  return (
    <ActionRequestsContext.Provider value={actions}>
      {children}
      {pending.length > 0 && (
        <TopbarActions>
          <Button
            variant="subtle"
            size="xs"
            leftSection={<IconBell size={16} />}
            onClick={() => void navigate("/actions")}
            aria-label={`Actions, ${pending.length} pending`}
          >
            <Group gap="xs" wrap="nowrap">
              Actions
              <Badge color="yellow" circle>
                {pending.length}
              </Badge>
            </Group>
          </Button>
        </TopbarActions>
      )}
    </ActionRequestsContext.Provider>
  );
}

/** Compact, collapsed-by-default review prompt beside the thread composer. */
export function ComposerPendingActions(): JSX.Element | null {
  const actions = useContext(ActionRequestsContext);
  const [expanded, setExpanded] = useState(false);
  const detailsId = useId();
  const pending = actions?.requests.filter((request) => request.state === "decision_pending") ?? [];

  useEffect(() => {
    if (pending.length === 0) setExpanded(false);
  }, [pending.length]);

  if (actions === null || pending.length === 0) return null;

  const summary = pending
    .slice(0, 2)
    .map((request) => `${request.action.group} / ${request.action.name} · ${request.title}`)
    .join(" · ");
  const extraCount = pending.length - 2;

  return (
    <Paper className="action-affordance-notice" withBorder p="xs" role="region" aria-label="Pending action approvals">
      <Stack gap="xs">
        <Group justify="space-between" align="center" wrap="nowrap">
          <div style={{ flex: 1, minWidth: 0 }}>
            <Text size="sm" fw={600}>
              {pending.length} action{pending.length === 1 ? "" : "s"} waiting for review
            </Text>
            <Text size="xs" c="dimmed" lineClamp={1}>
              {summary}
              {extraCount > 0 ? ` · +${extraCount} more` : ""}
            </Text>
          </div>
          <Button
            variant="subtle"
            size="xs"
            aria-label={expanded ? "Hide pending action details" : "Review pending actions"}
            aria-expanded={expanded}
            aria-controls={detailsId}
            onClick={() => setExpanded((current) => !current)}
          >
            {expanded ? "Hide details" : "Review"}
          </Button>
        </Group>
        <div id={detailsId} hidden={!expanded}>
          {expanded && (
            <Stack gap="sm">
              <StaleNotice streams={[actions.stream]} />
              {actions.error && (
                <Text role="alert" c="red">
                  {actions.error}
                </Text>
              )}
              {pending.map((request) => (
                <PendingActionCard
                  key={request.id}
                  request={request}
                  deciding={actions.deciding === request.id}
                  onDecide={actions.decide}
                />
              ))}
            </Stack>
          )}
        </div>
      </Stack>
    </Paper>
  );
}
