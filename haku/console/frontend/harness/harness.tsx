// Full-page screenshot harness for Haku Console. The production shell is rendered with mocked
// API data; the test's request fence answers the real iframe request with an unmistakable striped
// Haku UI document (mock_haku_ui.html) so layout overlap is visible in the resulting image.
import "./mock_api";

import { MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { createRoot } from "react-dom/client";

import { AgentNamesProvider } from "../agent_names";
import { ApprovalsEmbedPage } from "../approvals_embed_page";
import { HakuUiEmbed } from "../haku_ui_embed";
import type { ConsoleNavigationView, ConsoleView } from "../routing";
import { ShellChrome, type ShellChromeProps } from "../shell_chrome";
import { hakuTheme } from "../theme";
import { SAMPLE_PENDING, sampleRecentToolCalls } from "./sample_data";

const noop = () => {};
const noopNavigate = (_view: ConsoleNavigationView) => {};

const ENROLLMENT_ID = "10000000-0000-4000-8000-000000000001";

function ConsoleFixture({ view, reconnect = false }: { view: ConsoleView; reconnect?: boolean }) {
  return (
    <AgentNamesProvider>
      <HakuUiEmbed
        uiUrl="https://haku-ui.test/"
        launchAvailable
        view={view}
        agentEnrollmentId={view === "agentEnrollment" ? ENROLLMENT_ID : null}
        agentEnrollmentInitialChoice={reconnect ? "reconnect" : undefined}
        onNavigate={noopNavigate}
      />
    </AgentNamesProvider>
  );
}

const chromeProps: ShellChromeProps = {
  view: "embed",
  onNavigate: noopNavigate,
  approvalsOpen: false,
  onApprovalsOpenChange: noop,
  pendingApprovals: SAMPLE_PENDING,
  geolocationApprovals: [],
  screenshotApprovals: [],
  decidingApprovalIds: [],
  recentToolCalls: sampleRecentToolCalls(Date.now()),
  onApproveTool: noop,
  onDenyTool: noop,
  onApproveGeolocation: noop,
  onDenyGeolocation: noop,
  onApproveScreenshot: noop,
  onDenyScreenshot: noop,
  onDismissRecentToolCall: noop,
  syncError: null,
  syncing: false,
  lastSyncAt: new Date("2026-07-20T12:34:56-07:00"),
  geoGranted: true,
  tracking: true,
  onWithdrawGeolocation: noop,
  screenshotGranted: true,
  sharingScreen: true,
  onWithdrawScreenshot: noop,
  sessionExpiresAt: null,
  sessionExpiringSoon: false,
  onReauthenticate: noop,
};

function IndicatorFixture({ state }: { state: "current" | "syncing" | "error" }) {
  return (
    <div className="haku-console-shell">
      <ShellChrome
        {...chromeProps}
        syncError={state === "error" ? "Unauthorized" : null}
        syncing={state === "syncing"}
      />
      <main className="haku-shell-content" />
    </div>
  );
}

// The rail's session warning, which only appears inside the last few minutes of the session.
function SessionExpiringFixture() {
  return (
    <div className="haku-console-shell">
      <ShellChrome {...chromeProps} sessionExpiresAt={new Date(Date.now() + 4 * 60_000)} sessionExpiringSoon />
      <main className="haku-shell-content" />
    </div>
  );
}

function fixtureElement(fixture: string) {
  switch (fixture) {
    case "approvals-embed":
      return <ApprovalsEmbedPage />;
    case "settings":
      return <ConsoleFixture view="settings" />;
    case "agent-enrollment":
      return <ConsoleFixture view="agentEnrollment" />;
    case "agent-enrollment-reconnect":
      return <ConsoleFixture view="agentEnrollment" reconnect />;
    case "history":
    case "history-paged":
      return <ConsoleFixture view="toolCalls" />;
    case "sync-current":
      return <IndicatorFixture state="current" />;
    case "sync-syncing":
      return <IndicatorFixture state="syncing" />;
    case "sync-error":
      return <IndicatorFixture state="error" />;
    case "session-expiring":
      return <SessionExpiringFixture />;
    case "not-found":
      return <ConsoleFixture view="notFound" />;
    case "console":
      return <ConsoleFixture view="embed" />;
    default:
      throw new Error(`Unknown Haku fixture ${fixture}`);
  }
}

const fixture = (window as unknown as { __FIXTURE__?: string }).__FIXTURE__;
if (!fixture) throw new Error("Missing Haku fixture");
const colorScheme = (window as unknown as { __COLOR_SCHEME__?: "light" | "dark" }).__COLOR_SCHEME__ ?? "light";
const container = document.getElementById("app");
if (!container) throw new Error("missing #app");
createRoot(container).render(
  <MantineProvider forceColorScheme={colorScheme} theme={hakuTheme}>
    <Notifications position="top-right" />
    {fixtureElement(fixture)}
  </MantineProvider>
);
