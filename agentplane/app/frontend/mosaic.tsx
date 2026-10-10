import { ActionIcon, Badge, Box, Button, Group, Menu, Paper, Select, Stack, Text, Title } from "@mantine/core";
import IconPlus from "@tabler/icons-react/dist/esm/icons/IconPlus.mjs";
import IconX from "@tabler/icons-react/dist/esm/icons/IconX.mjs";
import { type JSX, useContext, useEffect, useMemo, useRef, useState } from "react";

import type { ActionRequestView } from "./actions/client";
import { ActionRequestDetail } from "./actions/detail";
import { ActionRequestsContext } from "./actions/requests";
import type { ThreadView } from "./client";
import { useRequiredThreadsLive } from "./live";
import { ProjectedSession } from "./threads/projected_session";
import { TopbarActions, TopbarTitle } from "./topbar";
import "./mosaic.css";

type MosaicPane = { kind: "thread"; id: string; threadId: string } | { kind: "action"; id: string; requestId: string };

function threadLabel(thread: Pick<ThreadView, "id" | "name">): string {
  return thread.name?.trim() || `Thread ${thread.id.slice(0, 8)}`;
}

function requestLabel(request: ActionRequestView | undefined): string {
  if (request === undefined) return "Action details";
  return request.title?.trim() || `${request.action.group} / ${request.action.name}`;
}

function MosaicActionPane({
  pane,
  label,
  index,
  active,
  onActivate,
  onClose,
}: {
  pane: Extract<MosaicPane, { kind: "action" }>;
  label: string;
  index: number;
  active: boolean;
  onActivate: () => void;
  onClose: () => void;
}): JSX.Element {
  return (
    <Paper
      withBorder
      className={`agentplane-mosaic-pane${active ? " is-active" : ""}`}
      data-mosaic-pane
      data-mosaic-pane-kind="action"
      data-pane-position={index}
      aria-label={`Action pane: ${label}`}
      onPointerDown={onActivate}
      onFocusCapture={onActivate}
    >
      <Group className="agentplane-mosaic-pane-header" gap="xs" wrap="nowrap">
        <Text size="sm" fw={600} lineClamp={1} style={{ flex: 1, minWidth: 0 }}>
          {label}
        </Text>
        <Badge size="xs" variant="light" color="gray">
          Action
        </Badge>
        <ActionIcon variant="subtle" aria-label="Close action pane" onClick={onClose}>
          <IconX size={16} />
        </ActionIcon>
      </Group>
      <Box className="agentplane-mosaic-pane-content">
        <ActionRequestDetail requestId={pane.requestId} embedded />
      </Box>
    </Paper>
  );
}

export function MosaicView(): JSX.Element {
  const threadsLive = useRequiredThreadsLive();
  const actions = useContext(ActionRequestsContext);
  const seeded = useRef(false);
  const [panes, setPanes] = useState<MosaicPane[]>([]);
  const [activePaneId, setActivePaneId] = useState<string | null>(null);
  const threads = threadsLive.snapshot?.threads ?? [];
  const requests = actions?.requests ?? [];
  const knownRequests = actions?.knownRequests;

  // A useful first view: two existing threads and, when one is waiting, its full detail pane.
  // After seeding, live additions remain available in the launcher without changing pane choice.
  useEffect(() => {
    if (seeded.current || threadsLive.snapshot === null || actions === null || actions.loading) return;
    const initial: MosaicPane[] = threadsLive.snapshot.threads
      .filter((thread) => !thread.archived)
      .slice(0, 2)
      .map((thread) => ({ kind: "thread", id: `thread:${thread.id}`, threadId: thread.id }));
    const firstPending = actions.requests.find((request) => request.state === "decision_pending");
    if (firstPending !== undefined) {
      initial.push({ kind: "action", id: `action:${firstPending.id}`, requestId: firstPending.id });
    }
    setPanes(initial);
    setActivePaneId(initial[0]?.id ?? null);
    seeded.current = true;
  }, [actions, threadsLive.snapshot]);

  const threadPanes = useMemo(
    () =>
      threads
        .filter((thread) => !thread.archived)
        .map((thread) => ({
          pane: { kind: "thread", id: `thread:${thread.id}`, threadId: thread.id } as const,
          label: threadLabel(thread),
        })),
    [threads]
  );
  const actionPanes = useMemo(
    () =>
      requests.map((request) => ({
        pane: { kind: "action", id: `action:${request.id}`, requestId: request.id } as const,
        label: requestLabel(request),
      })),
    [requests]
  );
  const paneLabel = (pane: MosaicPane): string =>
    pane.kind === "thread"
      ? threadLabel(threads.find((thread) => thread.id === pane.threadId) ?? { id: pane.threadId, name: null })
      : requestLabel(knownRequests?.get(pane.requestId) ?? requests.find((request) => request.id === pane.requestId));
  const addPane = (pane: MosaicPane): void => {
    setPanes((current) => (current.some((item) => item.id === pane.id) ? current : [...current, pane]));
    setActivePaneId(pane.id);
  };
  const removePane = (paneId: string): void => {
    setPanes((current) => {
      const next = current.filter((pane) => pane.id !== paneId);
      if (activePaneId === paneId) setActivePaneId(next[0]?.id ?? null);
      return next;
    });
  };

  return (
    <>
      <TopbarTitle>
        <Group gap="xs" wrap="nowrap">
          <Title order={1} size="h4">
            Mosaic preview
          </Title>
          <Badge size="xs" variant="light" color="gray">
            Experimental
          </Badge>
        </Group>
      </TopbarTitle>
      <TopbarActions>
        <Group gap="xs" wrap="nowrap">
          <Select
            aria-label="Active pane"
            placeholder="No active pane"
            value={activePaneId}
            data={panes.map((pane) => ({ value: pane.id, label: paneLabel(pane) }))}
            disabled={panes.length === 0}
            w={170}
            size="xs"
            onChange={setActivePaneId}
          />
          <Menu position="bottom-end" withinPortal>
            <Menu.Target>
              <Button size="xs" variant="light" leftSection={<IconPlus size={14} />}>
                Add pane
              </Button>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Label>Threads</Menu.Label>
              {threadPanes.length === 0 ? (
                <Menu.Item disabled>No open threads</Menu.Item>
              ) : (
                threadPanes.map(({ pane, label }) => (
                  <Menu.Item key={pane.id} onClick={() => addPane(pane)}>
                    {label}
                  </Menu.Item>
                ))
              )}
              <Menu.Divider />
              <Menu.Label>Pending actions</Menu.Label>
              {actionPanes.length === 0 ? (
                <Menu.Item disabled>{actions?.loading ? "Loading actions…" : "No pending actions"}</Menu.Item>
              ) : (
                actionPanes.map(({ pane, label }) => (
                  <Menu.Item key={pane.id} onClick={() => addPane(pane)}>
                    {label}
                  </Menu.Item>
                ))
              )}
            </Menu.Dropdown>
          </Menu>
        </Group>
      </TopbarActions>
      <section className="agentplane-mosaic" aria-label="Docked panes">
        {panes.length === 0 ? (
          <Stack align="center" justify="center" h="100%" c="dimmed">
            <Text>No threads or pending actions are available to open.</Text>
          </Stack>
        ) : (
          panes.map((pane, index) => {
            const label = paneLabel(pane);
            const active = pane.id === activePaneId;
            const className = `agentplane-mosaic-pane${active ? " is-active" : ""}`;
            if (pane.kind === "action") {
              return (
                <MosaicActionPane
                  key={pane.id}
                  pane={pane}
                  label={label}
                  index={index}
                  active={active}
                  onActivate={() => setActivePaneId(pane.id)}
                  onClose={() => removePane(pane.id)}
                />
              );
            }
            return (
              <Paper
                key={pane.id}
                withBorder
                className={className}
                data-mosaic-pane
                data-mosaic-pane-kind="thread"
                data-pane-position={index}
                aria-label={`Thread pane: ${label}`}
                onPointerDown={() => setActivePaneId(pane.id)}
                onFocusCapture={() => setActivePaneId(pane.id)}
              >
                <ProjectedSession threadId={pane.threadId} embedded onClose={() => removePane(pane.id)} />
              </Paper>
            );
          })
        )}
      </section>
    </>
  );
}
