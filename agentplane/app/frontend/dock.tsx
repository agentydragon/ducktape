/**
 * The dockable workspace at `/dock`: the Thread browser, the Actions queue, Action history, any
 * number of Threads and Action details as panes the operator drags into their own layout. Phone
 * width shows the same pane list one pane at a time.
 */
import { ActionIcon, Group, Menu, Select, Text, Tooltip } from "@mantine/core";
import {
  IconBox,
  IconHeartbeat,
  IconLayoutDashboard,
  IconLayoutSidebar,
  IconListCheck,
  IconSettings,
  IconX,
} from "./icons";
import {
  DockviewReact,
  type DockviewApi,
  type DockviewReadyEvent,
  type IDockviewPanelProps,
  type SerializedDockview,
} from "dockview-react";
import "dockview-react/dist/styles/dockview.css";
import { type JSX, createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";

import { ActionRequestDetail } from "./actions/detail";
import { ActionHistory } from "./actions/history";
import { ActionRequestsContext } from "./actions/requests";
import { ActionsAttentionBar, PendingActionsQueue } from "./actions/sidebar";
import "./dock.css";
import {
  closePane,
  isContentPane,
  loadWorkspace,
  openPane,
  paneId,
  saveWorkspace,
  type DockPane,
  type DockWorkspace,
} from "./dock_workspace";
import { useRequiredThreadsLive } from "./threads/live";
import { usePhoneWidth } from "./phone_width";
import { ThreadPage } from "./threads/page";
import { ThreadList } from "./threads/thread_list";
import { TopbarActions, TopbarContext, TopbarTitle, type TopbarSlots } from "./topbar";

interface DockActions {
  openPane: (pane: DockPane) => void;
  closePane: (id: string) => void;
}

const DockActionsContext = createContext<DockActions | null>(null);

function useDockActions(): DockActions {
  const actions = useContext(DockActionsContext);
  if (actions === null) throw new Error("A dock pane requires DockActionsContext");
  return actions;
}

const PANE_COMPONENT = "pane";
const BROWSER_PANE_WIDTH = 340;
const PANE_TOGGLES: readonly { pane: DockPane; label: string }[] = [
  { pane: { kind: "threads" }, label: "Threads" },
  { pane: { kind: "actions" }, label: "Actions queue" },
  { pane: { kind: "history" }, label: "Action history" },
];

/** What each open pane is called in its tab and in the phone pane picker. */
function usePaneLabels(panes: readonly DockPane[]): ReadonlyMap<string, string> {
  const threads = useRequiredThreadsLive().snapshot?.threads;
  const knownRequests = useContext(ActionRequestsContext)?.knownRequests;
  return useMemo(() => {
    const labels = new Map<string, string>();
    for (const pane of panes) {
      switch (pane.kind) {
        case "threads":
          labels.set(paneId(pane), "Threads");
          break;
        case "actions":
          labels.set(paneId(pane), "Actions");
          break;
        case "history":
          labels.set(paneId(pane), "History");
          break;
        case "thread":
          labels.set(
            paneId(pane),
            threads?.find((thread) => thread.id === pane.threadId)?.name?.trim() ||
              `Thread ${pane.threadId.slice(0, 8)}`
          );
          break;
        case "action":
          labels.set(paneId(pane), knownRequests?.get(pane.requestId)?.title.trim() || "Action details");
          break;
      }
    }
    return labels;
  }, [panes, threads, knownRequests]);
}

/** A pane that portals into the topbar slots (a Thread's title and controls) gets a header of its own. */
function PaneFrame({ children }: { children: JSX.Element }): JSX.Element {
  const [title, setTitle] = useState<HTMLDivElement | null>(null);
  const [actions, setActions] = useState<HTMLDivElement | null>(null);
  const slots = useMemo<TopbarSlots>(() => ({ title, actions }), [title, actions]);
  return (
    <TopbarContext.Provider value={slots}>
      <div className="agentplane-dock-pane-header">
        <div className="agentplane-dock-pane-title" ref={setTitle} />
        <div className="agentplane-dock-pane-actions" ref={setActions} />
      </div>
      <div className="agentplane-dock-pane-body">{children}</div>
    </TopbarContext.Provider>
  );
}

function PaneView({ pane }: { pane: DockPane }): JSX.Element {
  const dock = useDockActions();
  const id = paneId(pane);
  switch (pane.kind) {
    case "threads":
      return (
        <div className="agentplane-dock-scroll">
          <ThreadList
            detailed={false}
            onNavigate={() => undefined}
            onOpenThread={(thread) => dock.openPane({ kind: "thread", threadId: thread.id })}
          />
        </div>
      );
    case "actions":
      return (
        <div className="agentplane-dock-scroll agentplane-dock-actions">
          <PendingActionsQueue onOpenDetails={(request) => dock.openPane({ kind: "action", requestId: request.id })} />
        </div>
      );
    case "history":
      return (
        <div className="agentplane-dock-scroll agentplane-dock-padded">
          <ActionHistory embedded />
        </div>
      );
    case "action":
      return (
        <div className="agentplane-dock-scroll agentplane-dock-padded">
          <ActionRequestDetail requestId={pane.requestId} onDone={() => dock.closePane(id)} />
        </div>
      );
    case "thread":
      return (
        <PaneFrame>
          <ThreadPage key={pane.threadId} threadId={pane.threadId} embedded />
        </PaneFrame>
      );
  }
}

function PanelContent({ params }: IDockviewPanelProps<{ pane: DockPane }>): JSX.Element {
  return <PaneView pane={params.pane} />;
}

function EmptyDock(): JSX.Element {
  return (
    <div className="agentplane-dock-empty">
      <Text c="dimmed" size="sm">
        No panes open. Show Threads or Actions from the Panes menu.
      </Text>
    </div>
  );
}

const COMPONENTS = { [PANE_COMPONENT]: PanelContent };

/** Where a new pane lands: content panes tab together beside the browsers, browsers dock on the left. */
function addPanel(api: DockviewApi, pane: DockPane): void {
  const panels = api.panels;
  const panesOf = (predicate: (candidate: DockPane) => boolean): string[] =>
    panels.filter((panel) => predicate((panel.params as { pane: DockPane }).pane)).map((panel) => panel.id);
  const content = panesOf(isContentPane);
  const threads = panesOf((candidate) => candidate.kind === "threads");
  const options = { id: paneId(pane), component: PANE_COMPONENT, title: paneId(pane), params: { pane } };
  if (panels.length === 0) {
    api.addPanel(options);
  } else if (pane.kind === "threads") {
    api.addPanel({ ...options, position: { referencePanel: panels[0].id, direction: "left" } });
  } else if (pane.kind === "actions") {
    const anchor = threads[0] ?? panels[0].id;
    api.addPanel({
      ...options,
      position: { referencePanel: anchor, direction: threads.length > 0 ? "below" : "left" },
    });
  } else if (content.length > 0) {
    api.addPanel({ ...options, position: { referencePanel: content[content.length - 1] } });
  } else {
    // Relative to the whole grid, so the content column spans the full height beside the browsers.
    api.addPanel({ ...options, position: { direction: "right" } });
    api.getPanel("threads")?.group.api.setSize({ width: BROWSER_PANE_WIDTH });
  }
}

/** Makes the dock hold exactly `panes`; a no-op when it already does. */
function syncPanels(api: DockviewApi, panes: readonly DockPane[]): void {
  const wanted = new Set(panes.map(paneId));
  for (const panel of [...api.panels]) if (!wanted.has(panel.id)) api.removePanel(panel);
  for (const pane of panes) if (api.getPanel(paneId(pane)) === undefined) addPanel(api, pane);
}

function DesktopDock({
  workspace,
  setWorkspace,
  restoredLayout,
  onLayout,
  labels,
}: {
  workspace: DockWorkspace;
  setWorkspace: (update: (previous: DockWorkspace) => DockWorkspace) => void;
  restoredLayout: SerializedDockview | null;
  onLayout: (layout: SerializedDockview | null) => void;
  labels: ReadonlyMap<string, string>;
}): JSX.Element {
  const apiRef = useRef<DockviewApi | null>(null);
  const [ready, setReady] = useState(false);
  // The first render's workspace seeds the dock; later changes arrive through the effects below.
  const seed = useRef({ workspace, restoredLayout });

  const onReady = useCallback(
    (event: DockviewReadyEvent): void => {
      const api = event.api;
      apiRef.current = api;
      let restored = false;
      const { workspace: initial, restoredLayout: layout } = seed.current;
      if (layout !== null) {
        try {
          api.fromJSON(layout);
          restored = true;
        } catch (reason: unknown) {
          console.warn("dock: ignoring saved layout", reason);
          api.clear();
        }
      }
      syncPanels(api, initial.panes);
      if (!restored) api.getPanel("threads")?.group.api.setSize({ width: BROWSER_PANE_WIDTH });
      if (initial.activePaneId !== null) api.getPanel(initial.activePaneId)?.api.setActive();
      api.onDidActivePanelChange((change) => {
        const id = change.panel?.id;
        if (id !== undefined)
          setWorkspace((previous) => (previous.activePaneId === id ? previous : { ...previous, activePaneId: id }));
      });
      // A drag between groups removes and re-adds the panel; only a panel still absent afterwards is closed.
      api.onDidRemovePanel((panel) => {
        queueMicrotask(() => {
          if (api.getPanel(panel.id) === undefined) setWorkspace((previous) => closePane(previous, panel.id));
        });
      });
      api.onDidLayoutChange(() => onLayout(api.toJSON()));
      setReady(true);
    },
    [onLayout, setWorkspace]
  );

  useEffect(() => {
    const api = apiRef.current;
    if (!ready || api === null) return;
    syncPanels(api, workspace.panes);
    if (workspace.activePaneId !== null) api.getPanel(workspace.activePaneId)?.api.setActive();
  }, [ready, workspace.panes, workspace.activePaneId]);

  useEffect(() => {
    const api = apiRef.current;
    if (!ready || api === null) return;
    for (const pane of workspace.panes)
      api.getPanel(paneId(pane))?.api.setTitle(labels.get(paneId(pane)) ?? paneId(pane));
  }, [ready, workspace.panes, labels]);

  return (
    <DockviewReact
      className="agentplane-dock-theme"
      defaultRenderer="always"
      components={COMPONENTS}
      watermarkComponent={EmptyDock}
      onReady={onReady}
    />
  );
}

function PhoneDock({
  workspace,
  labels,
  onSelect,
}: {
  workspace: DockWorkspace;
  labels: ReadonlyMap<string, string>;
  onSelect: (id: string) => void;
}): JSX.Element {
  const dock = useDockActions();
  const active = workspace.panes.find((pane) => paneId(pane) === workspace.activePaneId) ?? null;
  return (
    <div className="agentplane-dock-phone">
      <Group className="agentplane-dock-phone-bar" gap="xs" wrap="nowrap">
        <Select
          aria-label="Open pane"
          size="xs"
          style={{ flex: 1, minWidth: 0 }}
          allowDeselect={false}
          comboboxProps={{ withinPortal: true }}
          data={workspace.panes.map((pane) => ({
            value: paneId(pane),
            label: labels.get(paneId(pane)) ?? paneId(pane),
          }))}
          value={workspace.activePaneId}
          placeholder="No panes open"
          onChange={(id) => id !== null && onSelect(id)}
        />
        {active !== null && (
          <ActionIcon
            variant="subtle"
            aria-label={`Close ${labels.get(paneId(active)) ?? "pane"}`}
            onClick={() => dock.closePane(paneId(active))}
          >
            <IconX size={16} />
          </ActionIcon>
        )}
      </Group>
      <div className="agentplane-dock-phone-content">
        {active === null ? <EmptyDock /> : <PaneView pane={active} />}
      </div>
    </div>
  );
}

function PanesMenu({
  workspace,
  onOpenSettings,
}: {
  workspace: DockWorkspace;
  onOpenSettings: () => void;
}): JSX.Element {
  const dock = useDockActions();
  const navigate = useNavigate();
  return (
    <Group gap={2} wrap="nowrap">
      <Menu position="bottom-end" withinPortal>
        <Menu.Target>
          <Tooltip label="Panes" withArrow>
            <ActionIcon variant="subtle" aria-label="Panes">
              <IconLayoutDashboard size={18} />
            </ActionIcon>
          </Tooltip>
        </Menu.Target>
        <Menu.Dropdown>
          <Menu.Label>Show</Menu.Label>
          {PANE_TOGGLES.map(({ pane, label }) => (
            <Menu.Item
              key={paneId(pane)}
              onClick={() => dock.openPane(pane)}
              rightSection={workspace.panes.some((open) => paneId(open) === paneId(pane)) ? "open" : undefined}
            >
              {label}
            </Menu.Item>
          ))}
          <Menu.Divider />
          <Menu.Label>Go to</Menu.Label>
          <Menu.Item leftSection={<IconBox size={14} />} onClick={() => void navigate("/sandboxes")}>
            Sandboxes
          </Menu.Item>
          <Menu.Item leftSection={<IconListCheck size={14} />} onClick={() => void navigate("/actions")}>
            All actions
          </Menu.Item>
          <Menu.Item leftSection={<IconHeartbeat size={14} />} onClick={() => void navigate("/system")}>
            System status
          </Menu.Item>
          <Menu.Item leftSection={<IconSettings size={14} />} onClick={onOpenSettings}>
            Settings
          </Menu.Item>
          <Menu.Item leftSection={<IconLayoutSidebar size={14} />} onClick={() => void navigate("/")}>
            Classic view
          </Menu.Item>
        </Menu.Dropdown>
      </Menu>
    </Group>
  );
}

export function DockView({ onOpenSettings }: { onOpenSettings: () => void }): JSX.Element {
  const phone = usePhoneWidth();
  const [workspace, setWorkspace] = useState<DockWorkspace>(loadWorkspace);
  const layoutRef = useRef<SerializedDockview | null>(workspace.layout);
  const workspaceRef = useRef(workspace);
  const labels = usePaneLabels(workspace.panes);
  const actionsPaneOpen = workspace.panes.some((pane) => pane.kind === "actions");

  useEffect(() => {
    workspaceRef.current = workspace;
    saveWorkspace({ ...workspace, layout: layoutRef.current });
  }, [workspace]);

  const saveLayout = useCallback((layout: SerializedDockview | null): void => {
    layoutRef.current = layout;
    saveWorkspace({ ...workspaceRef.current, layout });
  }, []);

  const dockActions = useMemo<DockActions>(
    () => ({
      openPane: (pane) => setWorkspace((previous) => openPane(previous, pane)),
      closePane: (id) => setWorkspace((previous) => closePane(previous, id)),
    }),
    []
  );

  return (
    <DockActionsContext.Provider value={dockActions}>
      <TopbarTitle>
        <Text fw={600} size="sm">
          Workspace
        </Text>
      </TopbarTitle>
      <TopbarActions>
        <PanesMenu workspace={workspace} onOpenSettings={onOpenSettings} />
      </TopbarActions>
      <ActionsAttentionBar visible={!actionsPaneOpen} />
      {phone ? (
        <PhoneDock
          workspace={workspace}
          labels={labels}
          onSelect={(id) => setWorkspace((previous) => ({ ...previous, activePaneId: id }))}
        />
      ) : (
        <div className="agentplane-dock-desktop">
          <DesktopDock
            workspace={workspace}
            setWorkspace={setWorkspace}
            restoredLayout={layoutRef.current}
            onLayout={saveLayout}
            labels={labels}
          />
        </div>
      )}
    </DockActionsContext.Provider>
  );
}
