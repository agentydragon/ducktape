import {
  ActionIcon,
  Badge,
  Box,
  Button,
  Group,
  Menu,
  Paper,
  ScrollArea,
  Select,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import IconGripVertical from "@tabler/icons-react/dist/esm/icons/IconGripVertical.mjs";
import IconPlus from "@tabler/icons-react/dist/esm/icons/IconPlus.mjs";
import IconX from "@tabler/icons-react/dist/esm/icons/IconX.mjs";
import {
  type CSSProperties,
  type JSX,
  type PointerEvent,
  type ReactNode,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";

import { ActionHistory } from "./actions/history";
import type { ActionRequestView } from "./actions/client";
import { ActionRequestDetail } from "./actions/detail";
import { ActionRequests, ActionRequestsContext } from "./actions/requests";
import type { ThreadView } from "./client";
import { useRequiredThreadsLive } from "./live";
import {
  createDefaultLayout,
  dockPane,
  paneIds,
  parseWorkspaceJson,
  removePaneFromLayout,
  resizeSplit,
  serializeWorkspace,
  type MosaicDockEdge,
  type MosaicLayoutNode,
  type MosaicPane,
  type MosaicWorkspace,
} from "./mosaic_layout";
import { ProjectedSession } from "./threads/projected_session";
import { snapshotFresh, threadStatusFromSnapshot } from "./thread_status";
import { ThreadStatusIndicator } from "./thread_status_indicator";
import { TopbarActions, TopbarTitle } from "./topbar";
import "./mosaic.css";

const MOSAIC_WORKSPACE_KEY = "agentplane-mosaic-workspace-v1";

interface DropTarget {
  paneId: string;
  edge: MosaicDockEdge;
}

function readStoredWorkspace(): MosaicWorkspace | null {
  if (typeof window === "undefined") return null;
  try {
    return parseWorkspaceJson(window.localStorage.getItem(MOSAIC_WORKSPACE_KEY));
  } catch (reason: unknown) {
    console.warn("mosaic: failed to read saved pane layout", reason);
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

function dropTargetAtPoint(document: Document, x: number, y: number, sourcePaneId: string): DropTarget | null {
  const target = document.elementFromPoint(x, y)?.closest<HTMLElement>("[data-mosaic-pane]");
  const paneId = target?.dataset.mosaicPaneId;
  if (target === null || target === undefined || paneId === undefined || paneId === sourcePaneId) return null;
  const rect = target.getBoundingClientRect();
  if (rect.width === 0 || rect.height === 0) return null;
  const horizontal = (x - rect.left) / rect.width;
  const vertical = (y - rect.top) / rect.height;
  const distances: Array<[MosaicDockEdge, number]> = [
    ["left", horizontal],
    ["right", 1 - horizontal],
    ["top", vertical],
    ["bottom", 1 - vertical],
  ];
  const edge = distances.reduce((closest, candidate) => (candidate[1] < closest[1] ? candidate : closest))[0];
  return { paneId, edge };
}

function MosaicPaneGrip({
  paneId,
  label,
  onActivate,
  onDragStart,
  onDragMove,
  onDragEnd,
  onDragCancel,
}: {
  paneId: string;
  label: string;
  onActivate: () => void;
  onDragStart: (paneId: string) => void;
  onDragMove: (paneId: string, document: Document, x: number, y: number) => void;
  onDragEnd: (paneId: string) => void;
  onDragCancel: () => void;
}): JSX.Element {
  const pointerId = useRef<number | null>(null);

  function handlePointerDown(event: PointerEvent<HTMLButtonElement>): void {
    event.preventDefault();
    event.stopPropagation();
    pointerId.current = event.pointerId;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    onActivate();
    onDragStart(paneId);
  }

  function handlePointerMove(event: PointerEvent<HTMLButtonElement>): void {
    if (pointerId.current !== event.pointerId) return;
    onDragMove(paneId, event.currentTarget.ownerDocument, event.clientX, event.clientY);
  }

  function finish(event: PointerEvent<HTMLButtonElement>): void {
    if (pointerId.current !== event.pointerId) return;
    pointerId.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    onDragEnd(paneId);
  }

  function cancel(event: PointerEvent<HTMLButtonElement>): void {
    if (pointerId.current !== event.pointerId) return;
    pointerId.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    onDragCancel();
  }

  return (
    <ActionIcon
      className="agentplane-mosaic-pane-grip"
      variant="subtle"
      size="sm"
      aria-label={`Drag to dock ${label}`}
      title="Drag to a pane edge to dock"
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={finish}
      onPointerCancel={cancel}
      onClick={(event) => event.stopPropagation()}
    >
      <IconGripVertical size={16} />
    </ActionIcon>
  );
}

function MosaicResizeHandle({
  split,
  onResize,
}: {
  split: Extract<MosaicLayoutNode, { type: "split" }>;
  onResize: (splitId: string, ratio: number) => void;
}): JSX.Element {
  const drag = useRef<{ pointerId: number; coordinate: number; length: number; ratio: number } | null>(null);
  const orientation = split.direction === "horizontal" ? "vertical" : "horizontal";

  function handlePointerDown(event: PointerEvent<HTMLDivElement>): void {
    const rect = event.currentTarget.parentElement?.getBoundingClientRect();
    if (rect === undefined) return;
    const horizontal = split.direction === "horizontal";
    drag.current = {
      pointerId: event.pointerId,
      coordinate: horizontal ? event.clientX : event.clientY,
      length: horizontal ? rect.width : rect.height,
      ratio: split.ratio,
    };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }

  function handlePointerMove(event: PointerEvent<HTMLDivElement>): void {
    const start = drag.current;
    if (start === null || start.pointerId !== event.pointerId || start.length <= 0) return;
    const coordinate = split.direction === "horizontal" ? event.clientX : event.clientY;
    onResize(split.id, start.ratio + (coordinate - start.coordinate) / start.length);
  }

  function finish(event: PointerEvent<HTMLDivElement>): void {
    if (drag.current?.pointerId !== event.pointerId) return;
    drag.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  }

  return (
    <div
      className="agentplane-mosaic-resize-handle"
      role="separator"
      aria-orientation={orientation}
      aria-label="Resize docked panes"
      aria-controls={`agentplane-mosaic-${split.id}`}
      aria-valuenow={Math.round(split.ratio * 100)}
      aria-valuemin={15}
      aria-valuemax={85}
      tabIndex={0}
      data-mosaic-resize={split.direction}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={finish}
      onPointerCancel={finish}
      onKeyDown={(event) => {
        const decrement = split.direction === "horizontal" ? event.key === "ArrowLeft" : event.key === "ArrowUp";
        const increment = split.direction === "horizontal" ? event.key === "ArrowRight" : event.key === "ArrowDown";
        if (!decrement && !increment) return;
        event.preventDefault();
        onResize(split.id, split.ratio + (increment ? 0.05 : -0.05));
      }}
    />
  );
}

function MosaicContentPane({
  pane,
  label,
  active,
  dropEdge,
  children,
  onActivate,
  onClose,
  onDragStart,
  onDragMove,
  onDragEnd,
  onDragCancel,
}: {
  pane: Exclude<MosaicPane, { kind: "thread" }>;
  label: string;
  active: boolean;
  dropEdge: MosaicDockEdge | null;
  children: ReactNode;
  onActivate: () => void;
  onClose: () => void;
  onDragStart: (paneId: string) => void;
  onDragMove: (paneId: string, document: Document, x: number, y: number) => void;
  onDragEnd: (paneId: string) => void;
  onDragCancel: () => void;
}): JSX.Element {
  const badgeLabel =
    pane.kind === "action"
      ? "Action"
      : pane.kind === "history"
        ? "History"
        : pane.kind === "threads"
          ? "Threads"
          : "Actions";
  return (
    <Paper
      withBorder
      className={`agentplane-mosaic-pane${active ? " is-active" : ""}`}
      data-mosaic-pane
      data-mosaic-pane-id={pane.id}
      data-mosaic-pane-kind={pane.kind}
      data-drop-edge={dropEdge ?? undefined}
      aria-label={`${pane.kind} pane: ${label}`}
      onPointerDown={onActivate}
      onFocusCapture={onActivate}
    >
      {dropEdge !== null && <div className="agentplane-mosaic-drop-preview" aria-hidden="true" />}
      <Group className="agentplane-mosaic-pane-header" gap="xs" wrap="nowrap">
        <MosaicPaneGrip
          paneId={pane.id}
          label={label}
          onActivate={onActivate}
          onDragStart={onDragStart}
          onDragMove={onDragMove}
          onDragEnd={onDragEnd}
          onDragCancel={onDragCancel}
        />
        <Text size="sm" fw={600} lineClamp={1} style={{ flex: 1, minWidth: 0 }}>
          {label}
        </Text>
        <Badge size="xs" variant="light" color="gray">
          {badgeLabel}
        </Badge>
        <ActionIcon variant="subtle" aria-label={`Close ${pane.kind} pane`} onClick={onClose}>
          <IconX size={16} />
        </ActionIcon>
      </Group>
      <Box className="agentplane-mosaic-pane-content">{children}</Box>
    </Paper>
  );
}

function MosaicThreadBrowserPane({
  openThreadIds,
  onOpenThread,
}: {
  openThreadIds: ReadonlySet<string>;
  onOpenThread: (thread: ThreadView) => void;
}): JSX.Element {
  const live = useRequiredThreadsLive();
  const [query, setQuery] = useState("");
  const fresh = snapshotFresh(live);
  const threads = live.snapshot?.threads ?? [];
  const sandboxes = new Map((live.snapshot?.sandboxes ?? []).map((sandbox) => [sandbox.name, sandbox]));
  const filtered = threads.filter((thread) => {
    const label = thread.name ?? thread.session_id;
    return `${label} ${thread.sandbox} ${thread.id}`.toLowerCase().includes(query.trim().toLowerCase());
  });

  return (
    <>
      <TextInput
        value={query}
        onChange={(event) => setQuery(event.currentTarget.value)}
        placeholder="Filter threads"
        aria-label="Filter threads"
        size="xs"
        mb="xs"
      />
      <ScrollArea className="agentplane-mosaic-thread-browser-scroll">
        <Stack gap={4}>
          {live.snapshot === null && (
            <Text c="dimmed" size="sm">
              Loading threads…
            </Text>
          )}
          {live.snapshot !== null && filtered.length === 0 && (
            <Text c="dimmed" size="sm">
              {threads.length === 0 ? "No threads yet." : "No matching threads."}
            </Text>
          )}
          {filtered.map((thread) => {
            const label = thread.name ?? thread.session_id;
            const status = threadStatusFromSnapshot(thread, sandboxes.get(thread.sandbox), fresh);
            const isOpen = openThreadIds.has(thread.id);
            return (
              <Button
                key={thread.id}
                variant={isOpen ? "light" : "subtle"}
                color={isOpen ? "blue" : "gray"}
                size="compact-sm"
                justify="flex-start"
                fullWidth
                leftSection={<ThreadStatusIndicator kind={status.kind} label={status.label} size="small" />}
                rightSection={
                  isOpen ? (
                    <Badge size="xs" variant="light">
                      Open
                    </Badge>
                  ) : undefined
                }
                onClick={() => onOpenThread(thread)}
                title={`${label} · ${thread.sandbox}`}
              >
                <span className="agentplane-mosaic-thread-name">{label}</span>
              </Button>
            );
          })}
        </Stack>
      </ScrollArea>
    </>
  );
}

export function MosaicView(): JSX.Element {
  const threadsLive = useRequiredThreadsLive();
  const actions = useContext(ActionRequestsContext);
  const [workspace, setWorkspace] = useState<MosaicWorkspace | null>(readStoredWorkspace);
  const [dropTarget, setDropTarget] = useState<DropTarget | null>(null);
  const dropTargetRef = useRef<DropTarget | null>(null);
  const [actionOriginByPane, setActionOriginByPane] = useState<ReadonlyMap<string, string>>(() => new Map());
  const [addPaneOpened, setAddPaneOpened] = useState(false);
  const [addPaneQuery, setAddPaneQuery] = useState("");
  const threads = threadsLive.snapshot?.threads ?? [];
  const requests = actions?.requests ?? [];
  const knownRequests = actions?.knownRequests;
  const currentWorkspace = workspace ?? { panes: [], layout: null, activePaneId: null };

  useEffect(() => {
    if (workspace !== null || threadsLive.snapshot === null || actions === null || actions.loading) return;
    const initial: MosaicPane[] = threadsLive.snapshot.threads
      .filter((thread) => !thread.archived)
      .slice(0, 2)
      .map((thread) => ({ kind: "thread", id: `thread:${thread.id}`, threadId: thread.id }));
    const firstPending = actions.requests.find((request) => request.state === "decision_pending");
    if (firstPending !== undefined) {
      initial.push({ kind: "action", id: `action:${firstPending.id}`, requestId: firstPending.id });
    }
    setWorkspace({
      panes: initial,
      layout: createDefaultLayout(initial.map((pane) => pane.id)),
      activePaneId: initial[0]?.id ?? null,
    });
  }, [actions, threadsLive.snapshot, workspace]);

  useEffect(() => {
    if (workspace === null) return;
    try {
      window.localStorage.setItem(MOSAIC_WORKSPACE_KEY, serializeWorkspace(workspace));
    } catch (reason: unknown) {
      console.warn("mosaic: failed to save pane layout", reason);
    }
  }, [workspace]);

  const threadPanes = threads
    .filter((thread) => !thread.archived)
    .map((thread) => ({
      pane: { kind: "thread", id: `thread:${thread.id}`, threadId: thread.id } as const,
      label: threadLabel(thread),
    }));
  const actionPanes = requests.map((request) => ({
    pane: { kind: "action", id: `action:${request.id}`, requestId: request.id } as const,
    label: requestLabel(request),
  }));
  const paneLabel = (pane: MosaicPane): string => {
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
          knownRequests?.get(pane.requestId) ?? requests.find((request) => request.id === pane.requestId)
        );
    }
  };
  const paneQuery = addPaneQuery.trim().toLowerCase();
  const visibleBuiltInPanes: MosaicPane[] = [
    { kind: "threads", id: "threads" },
    { kind: "actions", id: "actions" },
    { kind: "history", id: "history" },
  ].filter((pane) => paneLabel(pane).toLowerCase().includes(paneQuery));
  const visibleThreadPanes = threadPanes.filter(({ pane, label }) =>
    `${label} ${threads.find((thread) => thread.id === pane.threadId)?.sandbox ?? ""}`.toLowerCase().includes(paneQuery)
  );
  const visibleActionPanes = actionPanes.filter(({ label }) => label.toLowerCase().includes(paneQuery));

  function updateWorkspace(update: (current: MosaicWorkspace) => MosaicWorkspace): void {
    setWorkspace((current) => update(current ?? { panes: [], layout: null, activePaneId: null }));
  }

  function addPane(pane: MosaicPane): void {
    updateWorkspace((current) => {
      if (current.panes.some((item) => item.id === pane.id)) return { ...current, activePaneId: pane.id };
      const targetPaneId = current.activePaneId ?? paneIds(current.layout)[0];
      const layout: MosaicLayoutNode | null =
        current.layout === null || targetPaneId === undefined
          ? ({ type: "pane", paneId: pane.id } as const)
          : dockPane(current.layout, pane.id, targetPaneId, "right");
      return { panes: [...current.panes, pane], layout, activePaneId: pane.id };
    });
  }

  function openActionDetails(originPaneId: string, requestId: string): void {
    const pane: MosaicPane = { kind: "action", id: `action:${requestId}`, requestId };
    const origin = originPaneId === pane.id ? "actions" : originPaneId;
    setActionOriginByPane((current) => new Map(current).set(pane.id, origin));
    updateWorkspace((current) => {
      if (current.panes.some((item) => item.id === pane.id)) return { ...current, activePaneId: pane.id };
      const targetPaneId = current.panes.some((item) => item.id === origin)
        ? origin
        : (current.activePaneId ?? paneIds(current.layout)[0]);
      const layout: MosaicLayoutNode | null =
        current.layout === null || targetPaneId === undefined
          ? ({ type: "pane", paneId: pane.id } as const)
          : dockPane(current.layout, pane.id, targetPaneId, "right");
      return { panes: [...current.panes, pane], layout, activePaneId: pane.id };
    });
  }

  function removePane(paneId: string): void {
    const returnToPaneId = actionOriginByPane.get(paneId);
    updateWorkspace((current) => {
      const panes = current.panes.filter((pane) => pane.id !== paneId);
      const layout = removePaneFromLayout(current.layout, paneId);
      const remainingIds = paneIds(layout);
      const fallbackPaneId =
        (returnToPaneId !== undefined && remainingIds.includes(returnToPaneId) ? returnToPaneId : undefined) ??
        panes.find((pane) => pane.kind === "actions")?.id ??
        remainingIds[0] ??
        null;
      return {
        panes,
        layout,
        activePaneId: current.activePaneId === paneId ? fallbackPaneId : current.activePaneId,
      };
    });
    setActionOriginByPane((current) => {
      if (!current.has(paneId)) return current;
      const next = new Map(current);
      next.delete(paneId);
      return next;
    });
  }

  function activatePane(paneId: string): void {
    updateWorkspace((current) => ({ ...current, activePaneId: paneId }));
  }

  function handleDragMove(sourcePaneId: string, document: Document, x: number, y: number): void {
    const target = dropTargetAtPoint(document, x, y, sourcePaneId);
    dropTargetRef.current = target;
    setDropTarget(target);
  }

  function handleDragEnd(sourcePaneId: string): void {
    const target = dropTargetRef.current;
    dropTargetRef.current = null;
    setDropTarget(null);
    if (target === null) return;
    updateWorkspace((current) => ({
      ...current,
      layout: dockPane(current.layout, sourcePaneId, target.paneId, target.edge),
      activePaneId: sourcePaneId,
    }));
  }

  function handleDragCancel(): void {
    dropTargetRef.current = null;
    setDropTarget(null);
  }

  function resizeWorkspace(splitId: string, ratio: number): void {
    updateWorkspace((current) => ({ ...current, layout: resizeSplit(current.layout, splitId, ratio) }));
  }

  function renderPane(paneId: string): JSX.Element {
    const pane = currentWorkspace.panes.find((candidate) => candidate.id === paneId);
    if (pane === undefined) return <div className="agentplane-mosaic-pane-missing" />;
    const label = paneLabel(pane);
    const active = pane.id === currentWorkspace.activePaneId;
    const target = dropTarget?.paneId === pane.id ? dropTarget.edge : null;
    if (pane.kind === "thread") {
      return (
        <Paper
          key={pane.id}
          withBorder
          className={`agentplane-mosaic-pane${active ? " is-active" : ""}`}
          data-mosaic-pane
          data-mosaic-pane-id={pane.id}
          data-mosaic-pane-kind="thread"
          data-drop-edge={target ?? undefined}
          aria-label={`Thread pane: ${label}`}
          onPointerDown={() => activatePane(pane.id)}
          onFocusCapture={() => activatePane(pane.id)}
        >
          {target !== null && <div className="agentplane-mosaic-drop-preview" aria-hidden="true" />}
          <ProjectedSession
            threadId={pane.threadId}
            embedded
            embeddedHeaderActions={
              <MosaicPaneGrip
                paneId={pane.id}
                label={label}
                onActivate={() => activatePane(pane.id)}
                onDragStart={activatePane}
                onDragMove={handleDragMove}
                onDragEnd={handleDragEnd}
                onDragCancel={handleDragCancel}
              />
            }
            onClose={() => removePane(pane.id)}
          />
        </Paper>
      );
    }

    const sharedProps = {
      key: pane.id,
      pane,
      label,
      active,
      dropEdge: target,
      onActivate: () => activatePane(pane.id),
      onClose: () => removePane(pane.id),
      onDragStart: activatePane,
      onDragMove: handleDragMove,
      onDragEnd: handleDragEnd,
      onDragCancel: handleDragCancel,
    };

    let content: ReactNode;
    switch (pane.kind) {
      case "threads":
        content = (
          <MosaicThreadBrowserPane
            openThreadIds={
              new Set(
                currentWorkspace.panes
                  .filter((item): item is Extract<MosaicPane, { kind: "thread" }> => item.kind === "thread")
                  .map((item) => item.threadId)
              )
            }
            onOpenThread={(thread) => addPane({ kind: "thread", id: `thread:${thread.id}`, threadId: thread.id })}
          />
        );
        break;
      case "actions":
        content = <ActionRequests embedded onOpenDetails={(request) => openActionDetails(pane.id, request.id)} />;
        break;
      case "history":
        content = <ActionHistory embedded onOpenDetails={(request) => openActionDetails(pane.id, request.id)} />;
        break;
      case "action":
        content = (
          <ActionRequestDetail
            requestId={pane.requestId}
            embedded
            onBack={() => removePane(pane.id)}
            onResolved={() => removePane(pane.id)}
          />
        );
        break;
    }
    return (
      <MosaicContentPane {...sharedProps}>
        {pane.kind === "threads" ? <Box className="agentplane-mosaic-thread-browser">{content}</Box> : content}
      </MosaicContentPane>
    );
  }

  function renderLayout(node: MosaicLayoutNode): JSX.Element {
    if (node.type === "pane") return renderPane(node.paneId);
    const style: CSSProperties =
      node.direction === "horizontal"
        ? {
            gridTemplateColumns: `minmax(0, ${node.ratio}fr) 0.75rem minmax(0, ${1 - node.ratio}fr)`,
            gridTemplateRows: "minmax(0, 1fr)",
          }
        : {
            gridTemplateColumns: "minmax(0, 1fr)",
            gridTemplateRows: `minmax(0, ${node.ratio}fr) 0.75rem minmax(0, ${1 - node.ratio}fr)`,
          };
    return (
      <div
        id={`agentplane-mosaic-${node.id}`}
        className="agentplane-mosaic-split"
        data-direction={node.direction}
        style={style}
      >
        <div className="agentplane-mosaic-split-child" data-child="first">
          {renderLayout(node.first)}
        </div>
        <MosaicResizeHandle split={node} onResize={resizeWorkspace} />
        <div className="agentplane-mosaic-split-child" data-child="second">
          {renderLayout(node.second)}
        </div>
      </div>
    );
  }

  return (
    <>
      <TopbarTitle>
        <Group className="agentplane-mosaic-topbar-title" gap="xs" wrap="nowrap">
          <Title order={1} size="h4">
            <span className="agentplane-mosaic-title-desktop">Mosaic preview</span>
            <span className="agentplane-mosaic-title-mobile">Mosaic</span>
          </Title>
          <Badge className="agentplane-mosaic-experimental-badge" size="xs" variant="light" color="gray">
            Experimental
          </Badge>
        </Group>
      </TopbarTitle>
      <TopbarActions>
        <Group gap="xs" wrap="nowrap">
          <Select
            aria-label="Active pane"
            placeholder="No active pane"
            value={currentWorkspace.activePaneId}
            data={currentWorkspace.panes.map((pane) => ({ value: pane.id, label: paneLabel(pane) }))}
            disabled={currentWorkspace.panes.length === 0}
            w={170}
            size="xs"
            onChange={(paneId) => {
              if (paneId !== null) activatePane(paneId);
            }}
          />
          <Menu
            position="bottom-end"
            withinPortal
            opened={addPaneOpened}
            onChange={(opened) => {
              setAddPaneOpened(opened);
              if (!opened) setAddPaneQuery("");
            }}
          >
            <Menu.Target>
              <Button size="xs" variant="light" leftSection={<IconPlus size={14} />}>
                Add pane
              </Button>
            </Menu.Target>
            <Menu.Dropdown style={{ width: 320 }}>
              <TextInput
                aria-label="Find a pane or thread"
                placeholder="Find a pane or thread"
                value={addPaneQuery}
                onChange={(event) => setAddPaneQuery(event.currentTarget.value)}
                size="xs"
                mx="xs"
                mt="xs"
                mb="xs"
              />
              <ScrollArea mah={320} type="auto">
                {visibleBuiltInPanes.length > 0 && (
                  <>
                    <Menu.Label>Workspace panes</Menu.Label>
                    {visibleBuiltInPanes.map((pane) => (
                      <Menu.Item
                        key={pane.id}
                        onClick={() => {
                          addPane(pane);
                          setAddPaneOpened(false);
                        }}
                      >
                        {paneLabel(pane)}
                      </Menu.Item>
                    ))}
                  </>
                )}
                {visibleThreadPanes.length > 0 && (
                  <>
                    <Menu.Label>Threads</Menu.Label>
                    {visibleThreadPanes.map(({ pane, label }) => (
                      <Menu.Item
                        key={pane.id}
                        onClick={() => {
                          addPane(pane);
                          setAddPaneOpened(false);
                        }}
                        title={threads.find((thread) => thread.id === pane.threadId)?.sandbox}
                      >
                        {label}
                      </Menu.Item>
                    ))}
                  </>
                )}
                {visibleActionPanes.length > 0 && (
                  <>
                    <Menu.Label>Pending actions</Menu.Label>
                    {visibleActionPanes.map(({ pane, label }) => (
                      <Menu.Item
                        key={pane.id}
                        onClick={() => {
                          openActionDetails(currentWorkspace.activePaneId ?? "actions", pane.requestId);
                          setAddPaneOpened(false);
                        }}
                      >
                        {label}
                      </Menu.Item>
                    ))}
                  </>
                )}
                {visibleBuiltInPanes.length === 0 &&
                  visibleThreadPanes.length === 0 &&
                  visibleActionPanes.length === 0 && (
                    <Menu.Item disabled>
                      {actions?.loading && paneQuery.length === 0 ? "Loading actions…" : "No panes match this search"}
                    </Menu.Item>
                  )}
              </ScrollArea>
            </Menu.Dropdown>
          </Menu>
        </Group>
      </TopbarActions>
      <section className="agentplane-mosaic" aria-label="Docked panes">
        {currentWorkspace.layout === null ? (
          <Stack align="center" justify="center" h="100%" c="dimmed">
            <Text>Use Add pane to open Threads, Actions, or action history.</Text>
          </Stack>
        ) : (
          renderLayout(currentWorkspace.layout)
        )}
      </section>
    </>
  );
}
