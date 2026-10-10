import {
  ActionIcon,
  Badge,
  Button,
  Group,
  Menu,
  ScrollArea,
  Select,
  Text,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
import IconArrowLeft from "@tabler/icons-react/dist/esm/icons/IconArrowLeft.mjs";
import IconBox from "@tabler/icons-react/dist/esm/icons/IconBox.mjs";
import IconGripVertical from "@tabler/icons-react/dist/esm/icons/IconGripVertical.mjs";
import IconMenu2 from "@tabler/icons-react/dist/esm/icons/IconMenu2.mjs";
import IconPlus from "@tabler/icons-react/dist/esm/icons/IconPlus.mjs";
import IconX from "@tabler/icons-react/dist/esm/icons/IconX.mjs";
import { Mosaic, MosaicWindow, MosaicWindowContext, type MosaicNode, type MosaicPath } from "react-mosaic-component";
import "react-mosaic-component/react-mosaic-component.css";
import { type JSX, useContext, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router";

import type { ActionRequestView } from "./actions/client";
import { ActionRequestDetail } from "./actions/detail";
import { ActionHistory } from "./actions/history";
import { ActionRequestsContext } from "./actions/requests";
import { PendingActionsList } from "./actions/sidebar";
import type { ThreadView } from "./client";
import { useRequiredThreadsLive } from "./live";
import {
  createDefaultWorkspace,
  insertPane,
  paneIds,
  parseSplitLayout,
  parseWorkspaceJson,
  removePane,
  serializeWorkspace,
  type MosaicPane,
  type MosaicWorkspace,
} from "./mosaic_layout";
import { ThreadsBrowserContent } from "./sidebar";
import { ConnectionIndicator } from "./stream_status";
import { ProjectedSession } from "./threads/projected_session";
import { TopbarActions, TopbarTitle } from "./topbar";
import "./mosaic.css";

const WORKSPACE_KEY = "agentplane-mosaic-workspace-v1";
const PHONE_QUERY = "(max-width: 560px)";

function readWorkspace(): MosaicWorkspace | null {
  try {
    return parseWorkspaceJson(window.localStorage.getItem(WORKSPACE_KEY));
  } catch (reason: unknown) {
    console.warn("mosaic: failed to read saved workspace", reason);
    return null;
  }
}

function threadLabel(thread: Pick<ThreadView, "id" | "name">): string {
  return thread.name?.trim() || `Thread ${thread.id.slice(0, 8)}`;
}

function requestLabel(request: ActionRequestView | undefined): string {
  if (request === undefined) return "Action details";
  return request.title?.trim() || `${request.action.group} / ${request.action.name}`;
}

function MosaicDragGrip({ label }: { label: string }): JSX.Element {
  const { mosaicWindowActions } = useContext(MosaicWindowContext);
  return (
    mosaicWindowActions.connectDragSource(
      <span className="agentplane-mosaic-drag-grip">
        <ActionIcon variant="subtle" size="sm" aria-label={`Drag ${label} pane`}>
          <IconGripVertical size={16} />
        </ActionIcon>
      </span>
    ) ?? <></>
  );
}

function PaneContent({
  pane,
  openThreadIds,
  onOpenThread,
  onOpenDetails,
  onClose,
  phone,
}: {
  pane: MosaicPane;
  openThreadIds: ReadonlySet<string>;
  onOpenThread: (thread: ThreadView) => void;
  onOpenDetails: (request: ActionRequestView) => void;
  onClose: () => void;
  phone: boolean;
}): JSX.Element {
  switch (pane.kind) {
    case "threads":
      return (
        <ThreadsBrowserContent
          currentThreadId={null}
          openThreadIds={openThreadIds}
          onOpenThread={onOpenThread}
          onNavigate={() => undefined}
        />
      );
    case "actions":
      return <PendingActionsList onOpenDetails={onOpenDetails} />;
    case "history":
      return (
        <ScrollArea className="agentplane-mosaic-pane-scroll">
          <ActionHistory embedded onOpenDetails={onOpenDetails} />
        </ScrollArea>
      );
    case "action":
      return (
        <ScrollArea className="agentplane-mosaic-pane-scroll">
          <ActionRequestDetail requestId={pane.requestId} embedded onResolved={onClose} />
        </ScrollArea>
      );
    case "thread":
      return (
        <ProjectedSession
          threadId={pane.threadId}
          embedded
          embeddedHeaderActions={phone ? undefined : <MosaicDragGrip label="thread" />}
          onClose={onClose}
        />
      );
  }
}

function MosaicTile({
  pane,
  path,
  label,
  active,
  openThreadIds,
  onActivate,
  onClose,
  onOpenThread,
  onOpenDetails,
  onNewThread,
}: {
  pane: MosaicPane;
  path: MosaicPath;
  label: string;
  active: boolean;
  openThreadIds: ReadonlySet<string>;
  onActivate: () => void;
  onClose: () => void;
  onOpenThread: (thread: ThreadView) => void;
  onOpenDetails: (request: ActionRequestView) => void;
  onNewThread: () => void;
}): JSX.Element {
  const thread = pane.kind === "thread";
  const controls = (
    <Group gap={2} wrap="nowrap">
      {pane.kind === "threads" && (
        <Tooltip label="New thread via Sandboxes" withArrow>
          <ActionIcon variant="subtle" size="sm" aria-label="New thread via Sandboxes" onClick={onNewThread}>
            <IconPlus size={15} />
          </ActionIcon>
        </Tooltip>
      )}
      {pane.kind === "action" && (
        <ActionIcon variant="subtle" size="sm" aria-label="Back to previous pane" onClick={onClose}>
          <IconArrowLeft size={15} />
        </ActionIcon>
      )}
      {pane.kind !== "action" && (
        <ActionIcon variant="subtle" size="sm" aria-label={`Close ${label} pane`} onClick={onClose}>
          <IconX size={15} />
        </ActionIcon>
      )}
    </Group>
  );
  return (
    <MosaicWindow<string>
      path={path}
      title={label}
      className={`${thread ? "agentplane-mosaic-thread-tile" : ""}${active ? " agentplane-mosaic-active" : ""}`}
      toolbarControls={controls}
      draggable={!thread}
      renderToolbar={thread ? () => <div /> : undefined}
      onDragStart={onActivate}
    >
      <div
        className="agentplane-mosaic-tile-content"
        data-mosaic-pane
        data-mosaic-pane-id={pane.id}
        data-mosaic-pane-kind={pane.kind}
        onPointerDown={onActivate}
        onFocusCapture={onActivate}
      >
        <PaneContent
          pane={pane}
          openThreadIds={openThreadIds}
          onOpenThread={onOpenThread}
          onOpenDetails={onOpenDetails}
          onClose={onClose}
          phone={false}
        />
      </div>
    </MosaicWindow>
  );
}

/** A separate renderer uses the same workspace tree but gives phones one useful full-size tile. */
function MobileTile({
  pane,
  label,
  openThreadIds,
  onClose,
  onOpenThread,
  onOpenDetails,
  onNewThread,
}: {
  pane: MosaicPane;
  label: string;
  openThreadIds: ReadonlySet<string>;
  onClose: () => void;
  onOpenThread: (thread: ThreadView) => void;
  onOpenDetails: (request: ActionRequestView) => void;
  onNewThread: () => void;
}): JSX.Element {
  return (
    <div
      className="agentplane-mosaic-mobile-tile"
      data-mosaic-pane
      data-mosaic-pane-id={pane.id}
      data-mosaic-pane-kind={pane.kind}
    >
      {pane.kind !== "thread" && (
        <Group className="agentplane-mosaic-mobile-header" gap="xs" wrap="nowrap">
          <Text fw={600} size="sm" truncate style={{ flex: 1 }}>
            {label}
          </Text>
          {pane.kind === "threads" && (
            <ActionIcon aria-label="New thread via Sandboxes" variant="subtle" onClick={onNewThread}>
              <IconPlus size={15} />
            </ActionIcon>
          )}
          <ActionIcon
            aria-label={pane.kind === "action" ? "Back to previous pane" : `Close ${label} pane`}
            variant="subtle"
            onClick={onClose}
          >
            {pane.kind === "action" ? <IconArrowLeft size={15} /> : <IconX size={15} />}
          </ActionIcon>
        </Group>
      )}
      <PaneContent
        pane={pane}
        openThreadIds={openThreadIds}
        onOpenThread={onOpenThread}
        onOpenDetails={onOpenDetails}
        onClose={onClose}
        phone
      />
    </div>
  );
}

export function MosaicView({ onOpenSettings }: { onOpenSettings: () => void }): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const threadsLive = useRequiredThreadsLive();
  const actions = useContext(ActionRequestsContext);
  const [workspace, setWorkspace] = useState<MosaicWorkspace | null>(readWorkspace);
  const [phone, setPhone] = useState(() => window.matchMedia(PHONE_QUERY).matches);
  const [launcherOpen, setLauncherOpen] = useState(false);
  const [launcherQuery, setLauncherQuery] = useState("");
  const incomingConsumed = useRef(false);
  const threads = threadsLive.snapshot?.threads ?? [];
  const requests = actions?.requests ?? [];
  const current = workspace ?? { panes: [], layout: null, activePaneId: null, actionOrigins: {} };
  const openThreadIds = new Set(current.panes.flatMap((pane) => (pane.kind === "thread" ? [pane.threadId] : [])));

  useEffect(() => {
    const query = window.matchMedia(PHONE_QUERY);
    const onChange = (event: MediaQueryListEvent): void => setPhone(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    if (workspace !== null || threadsLive.snapshot === null) return;
    const first = threadsLive.snapshot.threads.find((thread) => !thread.archived);
    const state = location.state as { openThreadId?: unknown } | null;
    const incomingThread = typeof state?.openThreadId === "string" ? state.openThreadId : null;
    setWorkspace(createDefaultWorkspace(incomingThread ?? first?.id ?? null));
  }, [workspace, threadsLive.snapshot, location.state]);

  useEffect(() => {
    if (workspace === null) return;
    try {
      window.localStorage.setItem(WORKSPACE_KEY, serializeWorkspace(workspace));
    } catch (reason: unknown) {
      console.warn("mosaic: failed to save workspace", reason);
    }
  }, [workspace]);

  function labelFor(pane: MosaicPane): string {
    switch (pane.kind) {
      case "threads":
        return "Threads";
      case "actions":
        return "Actions";
      case "history":
        return "Action history";
      case "thread":
        return threadLabel(threads.find((thread) => thread.id === pane.threadId) ?? { id: pane.threadId, name: null });
      case "action":
        return requestLabel(
          actions?.knownRequests.get(pane.requestId) ?? requests.find((request) => request.id === pane.requestId)
        );
    }
  }

  function addPane(pane: MosaicPane, originPaneId?: string): void {
    setWorkspace((previous) => {
      const base = previous ?? createDefaultWorkspace(null);
      if (base.panes.some((item) => item.id === pane.id)) {
        return {
          ...base,
          activePaneId: pane.id,
          actionOrigins:
            pane.kind === "action" &&
            originPaneId &&
            originPaneId !== pane.id &&
            base.panes.some((item) => item.id === originPaneId)
              ? { ...base.actionOrigins, [pane.id]: originPaneId }
              : base.actionOrigins,
        };
      }
      const source =
        originPaneId && base.panes.some((item) => item.id === originPaneId) ? originPaneId : base.activePaneId;
      const contentPane = base.panes.find((item) => item.kind === "thread" || item.kind === "action");
      const anchor = (source === "threads" || source === "actions") && contentPane ? contentPane.id : source;
      const firstContentPane = contentPane === undefined && (pane.kind === "thread" || pane.kind === "action");
      return {
        ...base,
        panes: [...base.panes, pane],
        layout:
          firstContentPane && base.layout !== null
            ? {
                type: "split",
                direction: "row",
                children: [base.layout, pane.id],
                splitPercentages: [35, 65],
              }
            : insertPane(base.layout, anchor, pane.id),
        activePaneId: pane.id,
        actionOrigins:
          pane.kind === "action" && source !== null ? { ...base.actionOrigins, [pane.id]: source } : base.actionOrigins,
      };
    });
  }

  useEffect(() => {
    if (workspace === null || incomingConsumed.current) return;
    incomingConsumed.current = true;
    const state = location.state as { openThreadId?: unknown; openActionId?: unknown } | null;
    if (typeof state?.openThreadId === "string" && state.openThreadId.length > 0) {
      addPane({ kind: "thread", id: `thread:${state.openThreadId}`, threadId: state.openThreadId });
    } else if (typeof state?.openActionId === "string" && state.openActionId.length > 0) {
      addPane({ kind: "action", id: `action:${state.openActionId}`, requestId: state.openActionId }, "actions");
    }
  }, [workspace, location.state]);

  function closePane(paneId: string): void {
    setWorkspace((previous) => {
      if (previous === null) return previous;
      const panes = previous.panes.filter((pane) => pane.id !== paneId);
      const layout = removePane(previous.layout, paneId);
      const remaining = paneIds(layout);
      const origin = previous.actionOrigins[paneId];
      const fallback =
        (origin && remaining.includes(origin) ? origin : null) ??
        (remaining.includes("actions") ? "actions" : null) ??
        remaining[0] ??
        null;
      const actionOrigins = Object.fromEntries(
        Object.entries(previous.actionOrigins).filter(([detail, source]) => detail !== paneId && source !== paneId)
      );
      return {
        ...previous,
        panes,
        layout,
        activePaneId: previous.activePaneId === paneId ? fallback : previous.activePaneId,
        actionOrigins,
      };
    });
  }

  function openDetails(origin: string, request: ActionRequestView): void {
    addPane({ kind: "action", id: `action:${request.id}`, requestId: request.id }, origin);
  }

  function acceptLayout(next: MosaicNode<string> | null): void {
    setWorkspace((previous) => {
      if (previous === null) return previous;
      if (next === null) return previous.panes.length === 0 ? { ...previous, layout: null } : previous;
      const splitOnly = parseSplitLayout(next);
      if (splitOnly === null) return previous;
      const ids = paneIds(splitOnly);
      if (
        ids.length !== previous.panes.length ||
        new Set(ids).size !== ids.length ||
        previous.panes.some((pane) => !ids.includes(pane.id))
      )
        return previous;
      return { ...previous, layout: splitOnly };
    });
  }

  const builtIns: MosaicPane[] = [
    { kind: "threads", id: "threads" },
    { kind: "actions", id: "actions" },
    { kind: "history", id: "history" },
  ];
  const query = launcherQuery.trim().toLowerCase();
  const options: MosaicPane[] = [
    ...builtIns,
    ...threads.map((thread) => ({ kind: "thread" as const, id: `thread:${thread.id}`, threadId: thread.id })),
    ...requests.map((request) => ({ kind: "action" as const, id: `action:${request.id}`, requestId: request.id })),
  ].filter((pane) => labelFor(pane).toLowerCase().includes(query));
  const activePane = current.panes.find((pane) => pane.id === current.activePaneId) ?? current.panes[0];

  return (
    <>
      <TopbarTitle>
        <Group gap="xs" wrap="nowrap">
          <Title order={1} size="h4">
            Mosaic
          </Title>
          <Badge className="agentplane-mosaic-experimental-badge" size="xs" variant="light" color="gray">
            Experimental
          </Badge>
        </Group>
      </TopbarTitle>
      <TopbarActions>
        <Group gap="xs" wrap="nowrap">
          <ConnectionIndicator />
          {phone && (
            <Select
              aria-label="Active pane"
              placeholder="No pane"
              value={activePane?.id ?? null}
              data={current.panes.map((pane) => ({ value: pane.id, label: labelFor(pane) }))}
              size="xs"
              w={140}
              onChange={(value) => {
                if (value !== null) setWorkspace((previous) => previous && { ...previous, activePaneId: value });
              }}
            />
          )}
          <Menu opened={launcherOpen} onChange={setLauncherOpen} position="bottom-end" withinPortal>
            <Menu.Target>
              <Button size="xs" variant="light" leftSection={<IconPlus size={14} />}>
                Add pane
              </Button>
            </Menu.Target>
            <Menu.Dropdown className="agentplane-mosaic-launcher">
              <TextInput
                aria-label="Find a pane or thread"
                placeholder="Find a pane or thread"
                value={launcherQuery}
                onChange={(event) => setLauncherQuery(event.currentTarget.value)}
                size="xs"
                mx="xs"
                mt="xs"
                mb="xs"
              />
              <ScrollArea mah={320}>
                {options.length === 0 && <Menu.Item disabled>No panes match this search</Menu.Item>}
                {options.map((pane) => (
                  <Menu.Item
                    key={pane.id}
                    onClick={() => {
                      if (pane.kind === "action") addPane(pane, current.activePaneId ?? "actions");
                      else addPane(pane);
                      setLauncherOpen(false);
                      setLauncherQuery("");
                    }}
                  >
                    {labelFor(pane)}
                  </Menu.Item>
                ))}
              </ScrollArea>
            </Menu.Dropdown>
          </Menu>
          <Menu position="bottom-end" withinPortal>
            <Menu.Target>
              <ActionIcon variant="subtle" aria-label="Workspace navigation">
                <IconMenu2 size={18} />
              </ActionIcon>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Item leftSection={<IconBox size={15} />} onClick={() => void navigate("/sandboxes")}>
                Sandboxes
              </Menu.Item>
              <Menu.Item onClick={() => void navigate("/actions")}>Classic actions</Menu.Item>
              <Menu.Item onClick={() => void navigate("/")}>Classic view</Menu.Item>
              <Menu.Item onClick={onOpenSettings}>Settings</Menu.Item>
            </Menu.Dropdown>
          </Menu>
        </Group>
      </TopbarActions>
      <section className="agentplane-mosaic-workspace" aria-label="Docked panes">
        {workspace === null ? (
          <Text c="dimmed" className="agentplane-mosaic-empty">
            Loading workspace…
          </Text>
        ) : phone ? (
          activePane === undefined ? (
            <Text c="dimmed" className="agentplane-mosaic-empty">
              Use Add pane to open a view.
            </Text>
          ) : (
            <MobileTile
              pane={activePane}
              label={labelFor(activePane)}
              openThreadIds={openThreadIds}
              onClose={() => closePane(activePane.id)}
              onOpenThread={(thread) => addPane({ kind: "thread", id: `thread:${thread.id}`, threadId: thread.id })}
              onOpenDetails={(request) => openDetails(activePane.id, request)}
              onNewThread={() => void navigate("/sandboxes")}
            />
          )
        ) : (
          <Mosaic<string>
            value={current.layout}
            onChange={acceptLayout}
            className="agentplane-mosaic"
            resize={{ minimumPaneSizePx: { row: 180, column: 120 } }}
            zeroStateView={
              <Text c="dimmed" className="agentplane-mosaic-empty">
                Use Add pane to open a view.
              </Text>
            }
            renderTile={(paneId, path) => {
              const pane = current.panes.find((candidate) => candidate.id === paneId);
              if (pane === undefined) return <Text>Pane unavailable.</Text>;
              return (
                <MosaicTile
                  pane={pane}
                  path={path}
                  label={labelFor(pane)}
                  active={current.activePaneId === paneId}
                  openThreadIds={openThreadIds}
                  onActivate={() => setWorkspace((previous) => previous && { ...previous, activePaneId: paneId })}
                  onClose={() => closePane(paneId)}
                  onOpenThread={(thread) => addPane({ kind: "thread", id: `thread:${thread.id}`, threadId: thread.id })}
                  onOpenDetails={(request) => openDetails(paneId, request)}
                  onNewThread={() => void navigate("/sandboxes")}
                />
              );
            }}
          />
        )}
      </section>
    </>
  );
}
