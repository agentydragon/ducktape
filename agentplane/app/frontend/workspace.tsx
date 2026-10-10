import {
  ActionIcon,
  Badge,
  Button,
  Divider,
  Group,
  Popover,
  ScrollArea,
  Stack,
  Text,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
import IconArrowLeft from "@tabler/icons-react/dist/esm/icons/IconArrowLeft.mjs";
import IconArrowUpRight from "@tabler/icons-react/dist/esm/icons/IconArrowUpRight.mjs";
import IconLayoutDashboard from "@tabler/icons-react/dist/esm/icons/IconLayoutDashboard.mjs";
import IconListCheck from "@tabler/icons-react/dist/esm/icons/IconListCheck.mjs";
import IconPlus from "@tabler/icons-react/dist/esm/icons/IconPlus.mjs";
import IconX from "@tabler/icons-react/dist/esm/icons/IconX.mjs";
import { Mosaic, MosaicContext, MosaicWindow, type MosaicNode, type MosaicPath } from "react-mosaic-component";
import "react-mosaic-component/react-mosaic-component.css";
import { type JSX, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";

import type { ThreadView } from "./client";
import { ActionHistory } from "./actions/history";
import { ActionRequests } from "./actions/requests";
import { ActionRequestDetail } from "./actions/detail";
import { useRequiredThreadsLive } from "./live";
import { ProjectedSession } from "./threads/projected_session";
import { snapshotFresh, threadStatusFromSnapshot } from "./thread_status";
import { ThreadStatusIndicator } from "./thread_status_indicator";
import { TopbarActions, TopbarContext, TopbarTitle, type TopbarSlots } from "./topbar";
import "./workspace.css";

type PaneId = string;
type PaneKind = "threads" | "actions" | "history" | "thread" | "action";

const MOBILE_QUERY = "(max-width: 560px)";

function isPhoneWidth(): boolean {
  return window.matchMedia(MOBILE_QUERY).matches;
}

function paneKind(paneId: PaneId): PaneKind {
  if (paneId === "threads" || paneId === "actions" || paneId === "history") return paneId;
  if (paneId.startsWith("thread:")) return "thread";
  return "action";
}

function threadIdForPane(paneId: PaneId): string | null {
  return paneId.startsWith("thread:") ? paneId.slice("thread:".length) : null;
}

function actionIdForPane(paneId: PaneId): string | null {
  return paneId.startsWith("action:") ? paneId.slice("action:".length) : null;
}

function paneTitle(paneId: PaneId, threadNames: ReadonlyMap<string, string>): string {
  switch (paneKind(paneId)) {
    case "threads":
      return "Threads";
    case "actions":
      return "Actions";
    case "history":
      return "Action history";
    case "thread": {
      const threadId = threadIdForPane(paneId);
      return threadId === null ? "Thread" : (threadNames.get(threadId) ?? threadId);
    }
    case "action":
      return "Action details";
  }
}

function leaves(node: MosaicNode<PaneId> | null): PaneId[] {
  if (node === null) return [];
  if (typeof node === "string") return [node];
  if (node.type === "split") return node.children.flatMap(leaves);
  return node.tabs;
}

function addPaneBeside(node: MosaicNode<PaneId> | null, anchor: PaneId | null, added: PaneId): MosaicNode<PaneId> {
  if (node === null) return added;
  if (node === anchor) {
    return { type: "split", direction: "row", children: [node, added], splitPercentages: [50, 50] };
  }
  if (typeof node === "string") return node;
  if (node.type === "tabs") return node;
  let changed = false;
  const children = node.children.map((child) => {
    const next = addPaneBeside(child, anchor, added);
    if (next !== child) changed = true;
    return next;
  });
  return changed ? { ...node, children } : node;
}

function removePane(node: MosaicNode<PaneId> | null, target: PaneId): MosaicNode<PaneId> | null {
  if (node === null || node === target) return null;
  if (typeof node === "string") return node;
  if (node.type === "tabs") {
    const tabs = node.tabs.filter((tab) => tab !== target);
    if (tabs.length === 0) return null;
    if (tabs.length === 1) return tabs[0];
    return { ...node, tabs, activeTabIndex: Math.min(node.activeTabIndex, tabs.length - 1) };
  }

  const oldPercentages = node.splitPercentages ?? node.children.map(() => 100 / node.children.length);
  const remaining = node.children.flatMap((child, index) => {
    const next = removePane(child, target);
    return next === null ? [] : [{ node: next, percentage: oldPercentages[index] ?? 100 / node.children.length }];
  });
  const children = remaining.map((entry) => entry.node);
  if (children.length === 0) return null;
  if (children.length === 1) return children[0];
  const splitPercentages = remaining.map((entry) => entry.percentage);
  const sum = splitPercentages.reduce((total, percentage) => total + percentage, 0);
  return { ...node, children, splitPercentages: splitPercentages.map((percentage) => (percentage * 100) / sum) };
}

function equalizeSplits(node: MosaicNode<PaneId> | null): MosaicNode<PaneId> | null {
  if (node === null || typeof node === "string" || node.type === "tabs") return node;
  return {
    ...node,
    children: node.children.map((child) => equalizeSplits(child) ?? child),
    splitPercentages: node.children.map(() => 100 / node.children.length),
  };
}

function ExpandActiveMobilePane({ path, active, phone }: { path: MosaicPath; active: boolean; phone: boolean }): null {
  const { mosaicActions } = useContext(MosaicContext);
  const pathKey = path.join("/");
  useEffect(() => {
    if (!phone || !active) return;
    const stablePath = pathKey === "" ? [] : pathKey.split("/").map(Number);
    mosaicActions.expand(stablePath, 100);
  }, [active, mosaicActions, pathKey, phone]);
  return null;
}

function ThreadBrowserPane({
  onOpenThread,
  openThreadIds,
}: {
  onOpenThread: (thread: ThreadView) => void;
  openThreadIds: ReadonlySet<string>;
}): JSX.Element {
  const live = useRequiredThreadsLive();
  const [query, setQuery] = useState("");
  const fresh = snapshotFresh(live);
  const threads = live.snapshot?.threads ?? [];
  const sandboxes = new Map((live.snapshot?.sandboxes ?? []).map((sandbox) => [sandbox.name, sandbox]));
  const filtered = threads.filter((thread) => {
    const label = thread.name ?? thread.session_id;
    const sandbox = thread.sandbox;
    const haystack = `${label} ${sandbox} ${thread.id}`.toLowerCase();
    return haystack.includes(query.trim().toLowerCase());
  });

  return (
    <div className="agentplane-workspace-pane-content">
      <TextInput
        value={query}
        onChange={(event) => setQuery(event.currentTarget.value)}
        placeholder="Filter threads"
        aria-label="Filter threads"
        size="xs"
        mb="xs"
      />
      <ScrollArea className="agentplane-workspace-scroll">
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
                <span className="agentplane-workspace-thread-name">{label}</span>
              </Button>
            );
          })}
        </Stack>
      </ScrollArea>
    </div>
  );
}

function PaneContent({
  paneId,
  onAddPane,
  onOpenAction,
  onReturnFromAction,
  openThreadIds,
}: {
  paneId: PaneId;
  onAddPane: (paneId: PaneId) => void;
  onOpenAction: (originPaneId: PaneId, requestId: string) => void;
  onReturnFromAction: (paneId: PaneId) => void;
  openThreadIds: ReadonlySet<string>;
}): JSX.Element {
  const kind = paneKind(paneId);
  if (kind === "threads") {
    return (
      <ThreadBrowserPane onOpenThread={(thread) => onAddPane(`thread:${thread.id}`)} openThreadIds={openThreadIds} />
    );
  }
  if (kind === "thread") {
    const threadId = threadIdForPane(paneId);
    return threadId === null ? (
      <Text c="red">Thread pane has no thread ID.</Text>
    ) : (
      <ProjectedSession threadId={threadId} manageGlobalChrome={false} />
    );
  }
  if (kind === "actions") {
    return (
      <ScrollArea className="agentplane-workspace-pane-content">
        <ActionRequests embedded onOpenDetails={(request) => onOpenAction(paneId, request.id)} />
      </ScrollArea>
    );
  }
  if (kind === "history") {
    return (
      <ScrollArea className="agentplane-workspace-pane-content">
        <ActionHistory embedded onOpenDetails={(request) => onOpenAction(paneId, request.id)} />
      </ScrollArea>
    );
  }

  const requestId = actionIdForPane(paneId);
  if (requestId === null) return <Text c="red">Action pane has no request ID.</Text>;
  return (
    <ScrollArea className="agentplane-workspace-pane-content">
      <ActionRequestDetail
        requestId={requestId}
        onBack={() => onReturnFromAction(paneId)}
        onResolved={() => onReturnFromAction(paneId)}
      />
    </ScrollArea>
  );
}

function WorkspaceMosaicTile({
  paneId,
  path,
  title,
  active,
  phone,
  onAddPane,
  onOpenAction,
  onReturnFromAction,
  onActivate,
  onClose,
  openThreadIds,
}: {
  paneId: PaneId;
  path: MosaicPath;
  title: string;
  active: boolean;
  phone: boolean;
  onAddPane: (paneId: PaneId) => void;
  onOpenAction: (originPaneId: PaneId, requestId: string) => void;
  onReturnFromAction: (paneId: PaneId) => void;
  onActivate: () => void;
  onClose: () => void;
  openThreadIds: ReadonlySet<string>;
}): JSX.Element {
  const [threadActionsNode, setThreadActionsNode] = useState<HTMLDivElement | null>(null);
  const topbarSlots = useMemo<TopbarSlots>(
    () => ({ title: null, actions: paneKind(paneId) === "thread" ? threadActionsNode : null }),
    [paneId, threadActionsNode]
  );

  return (
    <TopbarContext.Provider value={topbarSlots}>
      <MosaicWindow<PaneId>
        path={path}
        title={title}
        className={active ? "agentplane-mosaic-pane-active" : ""}
        toolbarControls={
          <Group gap={2} wrap="nowrap">
            {paneKind(paneId) === "thread" && (
              <div className="agentplane-workspace-thread-actions" ref={setThreadActionsNode} />
            )}
            <Tooltip label="Make active pane" withArrow>
              <ActionIcon variant="subtle" size="sm" aria-label={`Make ${title} active`} onClick={onActivate}>
                <IconArrowUpRight size={14} />
              </ActionIcon>
            </Tooltip>
            <Tooltip label="Close pane" withArrow>
              <ActionIcon variant="subtle" size="sm" aria-label={`Close ${title}`} onClick={onClose}>
                <IconX size={14} />
              </ActionIcon>
            </Tooltip>
          </Group>
        }
      >
        <div
          className="agentplane-workspace-pane"
          data-active={active ? "true" : "false"}
          onPointerDown={onActivate}
          onFocusCapture={onActivate}
        >
          <ExpandActiveMobilePane path={path} active={active} phone={phone} />
          <PaneContent
            paneId={paneId}
            onAddPane={onAddPane}
            onOpenAction={onOpenAction}
            onReturnFromAction={onReturnFromAction}
            openThreadIds={openThreadIds}
          />
        </div>
      </MosaicWindow>
    </TopbarContext.Provider>
  );
}

export function WorkspaceRoute(): JSX.Element {
  const navigate = useNavigate();
  const live = useRequiredThreadsLive();
  const [layout, setLayout] = useState<MosaicNode<PaneId> | null>("threads");
  const [activePaneId, setActivePaneId] = useState<PaneId>("threads");
  const [originByActionPane, setOriginByActionPane] = useState<ReadonlyMap<PaneId, PaneId>>(() => new Map());
  const [launcherOpen, setLauncherOpen] = useState(false);
  const [launcherQuery, setLauncherQuery] = useState("");
  const [phone, setPhone] = useState(isPhoneWidth);
  const previousPhone = useRef(phone);
  const threads = useMemo(() => live.snapshot?.threads ?? [], [live.snapshot?.threads]);
  const threadNames = useMemo(
    () => new Map(threads.map((thread) => [thread.id, thread.name ?? thread.session_id])),
    [threads]
  );
  const paneIds = useMemo(() => leaves(layout), [layout]);
  const openThreadIds = useMemo(
    () => new Set(paneIds.map(threadIdForPane).filter((threadId): threadId is string => threadId !== null)),
    [paneIds]
  );
  const visibleThreads = threads.filter((thread) => {
    const label = thread.name ?? thread.session_id;
    return `${label} ${thread.sandbox} ${thread.id}`.toLowerCase().includes(launcherQuery.trim().toLowerCase());
  });

  useEffect(() => {
    const query = window.matchMedia(MOBILE_QUERY);
    const onChange = (event: MediaQueryListEvent): void => setPhone(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    if (previousPhone.current && !phone) setLayout((current) => equalizeSplits(current));
    previousPhone.current = phone;
  }, [phone]);

  useEffect(() => {
    if (paneIds.length > 0 && !paneIds.includes(activePaneId)) setActivePaneId(paneIds[0]);
  }, [activePaneId, paneIds]);

  function activateOrAdd(paneId: PaneId): void {
    const currentLeaves = leaves(layout);
    if (currentLeaves.includes(paneId)) {
      setActivePaneId(paneId);
    } else {
      setLayout((current) =>
        addPaneBeside(current, currentLeaves.includes(activePaneId) ? activePaneId : (currentLeaves[0] ?? null), paneId)
      );
      setActivePaneId(paneId);
    }
    setLauncherOpen(false);
    setLauncherQuery("");
  }

  function openActionDetails(originPaneId: PaneId, requestId: string): void {
    const detailPaneId = `action:${requestId}`;
    setOriginByActionPane((current) => new Map(current).set(detailPaneId, originPaneId));
    activateOrAdd(detailPaneId);
  }

  function closePane(paneId: PaneId): void {
    const next = removePane(layout, paneId);
    setLayout(next);
    const remaining = leaves(next);
    if (activePaneId === paneId) setActivePaneId(remaining[0] ?? "threads");
  }

  return (
    <div className={`agentplane-workspace${phone ? " agentplane-workspace-phone" : ""}`}>
      <TopbarTitle>
        <Group gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
          <IconLayoutDashboard size={18} aria-hidden="true" />
          <Title order={1} size="h5" className="agentplane-workspace-heading">
            Workspace
          </Title>
          <Badge variant="light" color="orange" size="sm">
            Preview
          </Badge>
        </Group>
      </TopbarTitle>
      <TopbarActions>
        <Button
          className="agentplane-workspace-classic-link"
          variant="subtle"
          size="compact-sm"
          leftSection={<IconArrowLeft size={14} />}
          onClick={() => void navigate("/")}
        >
          Classic view
        </Button>
        <Popover
          opened={launcherOpen}
          onChange={setLauncherOpen}
          position={phone ? "bottom-end" : "bottom-start"}
          width={320}
          shadow="md"
          withinPortal
        >
          <Popover.Target>
            <Button
              leftSection={<IconPlus size={15} />}
              size="compact-sm"
              onClick={() => setLauncherOpen((open) => !open)}
              aria-expanded={launcherOpen}
            >
              Add pane
            </Button>
          </Popover.Target>
          <Popover.Dropdown className="agentplane-workspace-launcher">
            <TextInput
              autoFocus
              value={launcherQuery}
              onChange={(event) => setLauncherQuery(event.currentTarget.value)}
              placeholder="Find a pane or thread"
              aria-label="Find a pane or thread"
              size="xs"
              mb="xs"
            />
            <Text size="xs" fw={700} c="dimmed" px={4} mb={4}>
              Panes
            </Text>
            <Stack gap={2}>
              {(["threads", "actions", "history"] as const)
                .filter((paneId) =>
                  paneTitle(paneId, threadNames).toLowerCase().includes(launcherQuery.trim().toLowerCase())
                )
                .map((paneId) => (
                  <Button
                    key={paneId}
                    variant="subtle"
                    size="compact-sm"
                    justify="flex-start"
                    onClick={() => activateOrAdd(paneId)}
                  >
                    {paneId === "actions" || paneId === "history" ? (
                      <IconListCheck size={15} />
                    ) : (
                      <IconLayoutDashboard size={15} />
                    )}
                    {paneTitle(paneId, threadNames)}
                  </Button>
                ))}
            </Stack>
            <Divider my="xs" />
            <Text size="xs" fw={700} c="dimmed" px={4} mb={4}>
              Threads
            </Text>
            <ScrollArea h={Math.min(300, Math.max(80, visibleThreads.length * 36))}>
              <Stack gap={2}>
                {visibleThreads.length === 0 ? (
                  <Text size="sm" c="dimmed" px="xs" py={4}>
                    No matching threads.
                  </Text>
                ) : (
                  visibleThreads.map((thread) => (
                    <Button
                      key={thread.id}
                      variant={openThreadIds.has(thread.id) ? "light" : "subtle"}
                      color={openThreadIds.has(thread.id) ? "blue" : "gray"}
                      size="compact-sm"
                      justify="flex-start"
                      fullWidth
                      onClick={() => activateOrAdd(`thread:${thread.id}`)}
                      rightSection={
                        <Text component="span" size="xs" c="dimmed">
                          {thread.sandbox}
                        </Text>
                      }
                      title={thread.sandbox}
                    >
                      <span className="agentplane-workspace-thread-name">{thread.name ?? thread.session_id}</span>
                    </Button>
                  ))
                )}
              </Stack>
            </ScrollArea>
          </Popover.Dropdown>
        </Popover>
      </TopbarActions>
      {phone && paneIds.length > 1 && (
        <div className="agentplane-workspace-phone-switcher">
          <Text size="xs" c="dimmed">
            Showing
          </Text>
          <select
            aria-label="Active pane"
            value={activePaneId}
            onChange={(event) => setActivePaneId(event.currentTarget.value)}
          >
            {paneIds.map((paneId) => (
              <option key={paneId} value={paneId}>
                {paneTitle(paneId, threadNames)}
              </option>
            ))}
          </select>
          <Tooltip label="Close active pane" withArrow>
            <ActionIcon variant="subtle" aria-label="Close active pane" onClick={() => closePane(activePaneId)}>
              <IconX size={15} />
            </ActionIcon>
          </Tooltip>
        </div>
      )}
      <div className="agentplane-workspace-canvas" data-testid="workspace-canvas">
        <Mosaic<PaneId>
          value={layout}
          onChange={setLayout}
          resize={{ minimumPaneSizePercentage: { row: 12, column: 18 }, minimumPaneSizePx: { row: 240, column: 180 } }}
          className="agentplane-mosaic"
          zeroStateView={<div className="agentplane-mosaic-zero" />}
          renderTile={(paneId, path) => (
            <WorkspaceMosaicTile
              paneId={paneId}
              path={path}
              title={paneTitle(paneId, threadNames)}
              active={activePaneId === paneId}
              phone={phone}
              onAddPane={activateOrAdd}
              onOpenAction={openActionDetails}
              onReturnFromAction={(actionPaneId) => {
                const origin = originByActionPane.get(actionPaneId);
                if (origin !== undefined && paneIds.includes(origin)) setActivePaneId(origin);
                else setActivePaneId("actions");
              }}
              onActivate={() => setActivePaneId(paneId)}
              onClose={() => closePane(paneId)}
              openThreadIds={openThreadIds}
            />
          )}
        />
        {layout === null && (
          <div className="agentplane-workspace-empty">
            <IconLayoutDashboard size={28} />
            <Text fw={600}>Your workspace is empty</Text>
            <Text size="sm" c="dimmed">
              Add a thread, Actions, or action history pane to get started.
            </Text>
            <Button leftSection={<IconPlus size={15} />} onClick={() => setLauncherOpen(true)}>
              Add pane
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
