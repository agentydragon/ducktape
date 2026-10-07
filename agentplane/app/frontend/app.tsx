import { ActionIcon, Anchor, Stack, Text, Title } from "@mantine/core";
// Per-icon subpaths, never the barrel: see tabler_icons.d.ts.
import IconMenu2 from "@tabler/icons-react/dist/esm/icons/IconMenu2.mjs";
import { type JSX, useCallback, useEffect, useMemo, useState } from "react";
import { HashRouter, Route, Routes, useLocation, useMatch, useNavigate, useParams } from "react-router";

import { ActionRequests } from "./actions/requests";
import { ActionAffordance } from "./actions/affordance";
import { ActionHistory } from "./actions/history";
import { ConnectionConsent } from "./consent";
import { SandboxPage } from "./sandbox_page";
import { SandboxesLiveProvider, ThreadsLiveProvider } from "./live";
import { SandboxList } from "./sandboxes";
import { ProjectedSession } from "./threads/projected_session";
import { Settings, type SettingsTab } from "./settings/dialog";
import { Sidebar } from "./sidebar";
import { electricThreadSync } from "./threads/thread_store";
import { ThreadSyncContext } from "./threads/thread_sync";
import { TopbarContext, TopbarTitle, type TopbarSlots } from "./topbar";
import { appDocumentTitle } from "./tab_metadata";
import "./shell.css";

// Hash routing: the API serves the bundle at "/" only, so no path has to reach the server.
function sandboxPath(name: string): string {
  return `/sandboxes/${encodeURIComponent(name)}`;
}

function required(value: string | undefined, name: string): string {
  if (value === undefined) throw new Error(`route parameter ${name} is missing`);
  return value;
}

/** Nothing selected: the sidebar carries the Threads list, so the landing pane just points at it. */
function ThreadsLanding(): JSX.Element {
  const navigate = useNavigate();
  // TODO: Give the "open Sandboxes" link an href so it is keyboard focusable.
  return (
    <Stack align="center" justify="center" h="100%">
      <Text c="dimmed">
        Select a thread from the sidebar, or{" "}
        <Anchor size="sm" onClick={() => void navigate("/sandboxes")}>
          open Sandboxes
        </Anchor>{" "}
        to start one.
      </Text>
    </Stack>
  );
}

function SandboxListRoute(): JSX.Element {
  const navigate = useNavigate();
  return <SandboxList onOpen={(name) => void navigate(sandboxPath(name))} />;
}

function ConsentRoute(): JSX.Element {
  const handle = required(useParams().handle, "handle");
  return <ConnectionConsent key={handle} handle={handle} />;
}

export function SandboxRoute(): JSX.Element {
  const name = required(useParams().name, "name");
  const navigate = useNavigate();
  return (
    <SandboxPage
      key={name}
      name={name}
      onBack={() => void navigate("/sandboxes")}
      onOpenThread={(threadId) => void navigate(`/threads/${encodeURIComponent(threadId)}`)}
    />
  );
}

function ActionsPage(): JSX.Element {
  return (
    <Stack>
      <TopbarTitle>
        <Title order={1} size="h4">
          Actions
        </Title>
      </TopbarTitle>
      <Text c="dimmed" size="sm">
        Review requests awaiting a decision, then browse their decision and execution history.
      </Text>
      <ActionRequests embedded />
      <ActionHistory embedded />
    </Stack>
  );
}

function ThreadRoute({ settingsOpen }: { settingsOpen: boolean }): JSX.Element {
  const threadId = required(useParams().threadId, "threadId");
  return <ProjectedSession key={threadId} threadId={threadId} settingsOpen={settingsOpen} />;
}

// Matches sidebar.css's phone breakpoint (max-width: 560px) from the other side.
const DESKTOP_SIDEBAR_QUERY = "(min-width: 561px)";

function AppRoutes(): JSX.Element {
  const location = useLocation();
  const threadRoute = useMatch("/threads/:threadId");
  // Not legacy-path compatibility: api.py's MCP-linkage OAuth callback redirects the browser here
  // on completion, and it needs to land showing the result rather than the Sandboxes list.
  const [settingsTab, setSettingsTab] = useState<SettingsTab | null>(() =>
    location.pathname === "/mcp-servers" ? "mcp-servers" : null
  );
  const routeTitle =
    settingsTab !== null
      ? appDocumentTitle(location.pathname, true)
      : threadRoute === null
        ? appDocumentTitle(location.pathname, false)
        : null;
  useEffect(() => {
    if (routeTitle !== null) document.title = routeTitle;
  }, [routeTitle]);
  // Open by default at desktop width, closed at phone width; the CSS media query then decides
  // whether "closed" means a collapsed-to-nothing column or a fully hidden overlay. Crossing the
  // breakpoint resets to that side's default -- an "open" docked column left over from a resize
  // would otherwise render, at phone width, as a full-screen overlay intercepting the whole page.
  const [sidebarOpen, setSidebarOpen] = useState(() => window.matchMedia(DESKTOP_SIDEBAR_QUERY).matches);
  useEffect(() => {
    const query = window.matchMedia(DESKTOP_SIDEBAR_QUERY);
    const onChange = (event: MediaQueryListEvent): void => setSidebarOpen(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);
  const [titleNode, setTitleNode] = useState<HTMLDivElement | null>(null);
  const [actionsNode, setActionsNode] = useState<HTMLDivElement | null>(null);
  // Stable identities so React attaches each ref once instead of on every render.
  const titleRef = useCallback((node: HTMLDivElement | null) => setTitleNode(node), []);
  const actionsRef = useCallback((node: HTMLDivElement | null) => setActionsNode(node), []);
  const topbarSlots = useMemo<TopbarSlots>(
    () => ({ title: titleNode, actions: actionsNode }),
    [titleNode, actionsNode]
  );
  const fullBleed = threadRoute !== null;
  const routes = (
    <Routes>
      <Route path="/" element={<ThreadsLanding />} />
      <Route path="/sandboxes" element={<SandboxListRoute />} />
      <Route path="/actions" element={<ActionsPage />} />
      <Route path="/actions/:requestId" element={<ActionRequests />} />
      <Route path="/connection-enrollments/:handle" element={<ConsentRoute />} />
      <Route path="/sandboxes/:name" element={<SandboxRoute />} />
      <Route path="/threads/:threadId" element={<ThreadRoute settingsOpen={settingsTab !== null} />} />
      <Route path="*" element={<ThreadsLanding />} />
    </Routes>
  );
  return (
    <div className="agentplane-shell">
      <Sidebar
        settingsOpen={settingsTab !== null}
        onOpenSettings={() => setSettingsTab("oauth-clients")}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
      />
      <div className={`agentplane-shell-main${fullBleed ? " agentplane-shell-fullbleed" : ""}`}>
        <div className="agentplane-topbar">
          <ActionIcon variant="subtle" aria-label="Toggle navigation" onClick={() => setSidebarOpen((open) => !open)}>
            <IconMenu2 size={18} />
          </ActionIcon>
          <div className="agentplane-topbar-title" ref={titleRef} />
          <div className="agentplane-topbar-actions" ref={actionsRef} />
        </div>
        <div className={`agentplane-shell-main-content${fullBleed ? " agentplane-shell-fullbleed" : ""}`}>
          <TopbarContext.Provider value={topbarSlots}>
            <ActionAffordance>
              {threadRoute !== null ? <SandboxesLiveProvider>{routes}</SandboxesLiveProvider> : routes}
            </ActionAffordance>
          </TopbarContext.Provider>
        </div>
      </div>
      <Settings
        opened={settingsTab !== null}
        tab={settingsTab ?? "oauth-clients"}
        onTabChange={setSettingsTab}
        onClose={() => setSettingsTab(null)}
      />
    </div>
  );
}

export default function App(): JSX.Element {
  return (
    <ThreadSyncContext.Provider value={electricThreadSync}>
      <HashRouter>
        <ThreadsLiveProvider>
          <AppRoutes />
        </ThreadsLiveProvider>
      </HashRouter>
    </ThreadSyncContext.Provider>
  );
}
