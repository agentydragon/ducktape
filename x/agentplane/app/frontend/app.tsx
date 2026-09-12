import { Button, Container, Group, Stack } from "@mantine/core";
import { useState } from "react";
import { HashRouter, Route, Routes, useLocation, useMatch, useNavigate, useParams } from "react-router";

import { ActionRequests } from "./actions";
import { ActionHistory } from "./actions_history";
import { ConnectionConsent } from "./consent";
import { SandboxPage } from "./sandbox_page";
import { SandboxList } from "./sandboxes";
import { SessionView } from "./session";
import { Settings, type SettingsTab } from "./settings";

// Hash routing: the API serves the bundle at "/" only, so no path has to reach the server.
function sandboxPath(name: string): string {
  return `/sandboxes/${encodeURIComponent(name)}`;
}

/**
 * The three pages the Settings modal replaced kept their own routes; a path among them still opens
 * the modal on the matching tab, so an old bookmark or the visual-test harness's `#/connections`
 * still lands somewhere sensible without a dedicated `<Route>`.
 */
const LEGACY_SETTINGS_PATH: Record<string, SettingsTab> = {
  "/connections": "oauth-clients",
  "/mcp-servers": "mcp-servers",
  "/notifications": "notifications",
};

function sessionPath(name: string, sessionId: string): string {
  return `${sandboxPath(name)}/sessions/${encodeURIComponent(sessionId)}`;
}

function required(value: string | undefined, name: string): string {
  if (value === undefined) throw new Error(`route parameter ${name} is missing`);
  return value;
}

function ListRoute(): JSX.Element {
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
      onBack={() => void navigate("/")}
      onOpenSession={(sessionId) => void navigate(sessionPath(name, sessionId))}
    />
  );
}

function SessionRoute(): JSX.Element {
  const params = useParams();
  const name = required(params.name, "name");
  const navigate = useNavigate();
  return (
    <SessionView
      key={`${name}/${required(params.sessionId, "sessionId")}`}
      sandbox={name}
      sessionId={required(params.sessionId, "sessionId")}
      onBack={() => void navigate(sandboxPath(name))}
    />
  );
}

function AppRoutes(): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const sessionRoute = useMatch("/sandboxes/:name/sessions/:sessionId");
  // Seeded from the initial path so a legacy link/bookmark (or the visual-test harness) opens
  // straight to the matching tab; afterwards the modal is plain local UI state, not routed.
  const [settingsTab, setSettingsTab] = useState<SettingsTab | null>(
    () => LEGACY_SETTINGS_PATH[location.pathname] ?? null
  );
  return (
    <Container size="xl" py="md" h={sessionRoute ? "100dvh" : undefined}>
      <Stack h="100%">
        <Group>
          <Button variant={location.pathname === "/" ? "filled" : "subtle"} onClick={() => void navigate("/")}>
            Sandboxes
          </Button>
          <Button
            variant={location.pathname === "/actions" ? "filled" : "subtle"}
            onClick={() => void navigate("/actions")}
          >
            Actions
          </Button>
          <Button
            variant={location.pathname === "/actions/history" ? "filled" : "subtle"}
            onClick={() => void navigate("/actions/history")}
          >
            Action history
          </Button>
          <Button variant={settingsTab ? "filled" : "subtle"} onClick={() => setSettingsTab("oauth-clients")}>
            Settings
          </Button>
        </Group>
        <Routes>
          <Route path="/" element={<ListRoute />} />
          <Route path="/actions" element={<ActionRequests />} />
          <Route path="/actions/history" element={<ActionHistory />} />
          <Route path="/actions/:requestId" element={<ActionRequests />} />
          <Route path="/connection-enrollments/:handle" element={<ConsentRoute />} />
          <Route path="/sandboxes/:name" element={<SandboxRoute />} />
          <Route path="/sandboxes/:name/sessions/:sessionId" element={<SessionRoute />} />
          <Route path="*" element={<ListRoute />} />
        </Routes>
        <Settings
          opened={settingsTab !== null}
          tab={settingsTab ?? "oauth-clients"}
          onTabChange={setSettingsTab}
          onClose={() => setSettingsTab(null)}
        />
      </Stack>
    </Container>
  );
}

export default function App(): JSX.Element {
  return (
    <HashRouter>
      <AppRoutes />
    </HashRouter>
  );
}
