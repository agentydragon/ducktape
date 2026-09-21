import { ActionIcon, Anchor, Stack, Text } from "@mantine/core";
// Per-icon subpaths, never the barrel: see tabler_icons.d.ts.
import IconMenu2 from "@tabler/icons-react/dist/esm/icons/IconMenu2.mjs";
import { type JSX, useState } from "react";
import { HashRouter, Route, Routes, useLocation, useMatch, useNavigate, useParams } from "react-router";

import { ActionRequests } from "./actions";
import { ActionHistory } from "./actions_history";
import { ConnectionConsent } from "./consent";
import { SandboxPage } from "./sandbox_page";
import { SandboxList } from "./sandboxes";
import { ProjectedSession } from "./projected_session";
import { Settings, type SettingsTab } from "./settings/dialog";
import { Sidebar } from "./sidebar";
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

function SandboxRoute(): JSX.Element {
  const name = required(useParams().name, "name");
  const navigate = useNavigate();
  return (
    <SandboxPage
      name={name}
      onBack={() => void navigate("/sandboxes")}
      onOpenThread={(threadId) => void navigate(`/threads/${encodeURIComponent(threadId)}`)}
    />
  );
}

function ThreadRoute(): JSX.Element {
  const threadId = required(useParams().threadId, "threadId");
  const navigate = useNavigate();
  return <ProjectedSession threadId={threadId} onBack={() => void navigate("/")} />;
}

function AppRoutes(): JSX.Element {
  const location = useLocation();
  const threadRoute = useMatch("/threads/:threadId");
  // Not legacy-path compatibility: api.py's MCP-linkage OAuth callback redirects the browser here
  // on completion, and it needs to land showing the result rather than the Sandboxes list.
  const [settingsTab, setSettingsTab] = useState<SettingsTab | null>(() =>
    location.pathname === "/mcp-servers" ? "mcp-servers" : null
  );
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const fullBleed = threadRoute !== null;
  return (
    <div className="agentplane-shell">
      <Sidebar
        settingsOpen={settingsTab !== null}
        onOpenSettings={() => setSettingsTab("oauth-clients")}
        mobileOpen={mobileSidebarOpen}
        onMobileClose={() => setMobileSidebarOpen(false)}
      />
      <div className={`agentplane-shell-main${fullBleed ? " agentplane-shell-fullbleed" : ""}`}>
        <div className="agentplane-mobile-topbar">
          <ActionIcon variant="subtle" aria-label="Open navigation" onClick={() => setMobileSidebarOpen(true)}>
            <IconMenu2 size={18} />
          </ActionIcon>
        </div>
        <div className={`agentplane-shell-main-content${fullBleed ? " agentplane-shell-fullbleed" : ""}`}>
          <Routes>
            <Route path="/" element={<ThreadsLanding />} />
            <Route path="/sandboxes" element={<SandboxListRoute />} />
            <Route path="/actions" element={<ActionRequests />} />
            <Route path="/actions/history" element={<ActionHistory />} />
            <Route path="/actions/:requestId" element={<ActionRequests />} />
            <Route path="/connection-enrollments/:handle" element={<ConsentRoute />} />
            <Route path="/sandboxes/:name" element={<SandboxRoute />} />
            <Route path="/threads/:threadId" element={<ThreadRoute />} />
            <Route path="*" element={<ThreadsLanding />} />
          </Routes>
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
    <HashRouter>
      <AppRoutes />
    </HashRouter>
  );
}
