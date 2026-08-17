// Full-page screenshot harness for Haku Console. The production shell is rendered with mocked
// API data; render.mjs intercepts the real iframe request and supplies an unmistakable striped
// Haku UI document so layout overlap is visible in the resulting image.
import "./mock_api";

import { MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { useEffect } from "react";
import { createRoot } from "react-dom/client";

import { HakuUiEmbed } from "../haku_ui_embed";
import { OAuthResultView } from "../oauth_result_page";
import type { ConsoleNavigationView, ConsoleView } from "../routing";
import { ShellChrome, type ShellChromeProps } from "../shell_chrome";
import { hakuTheme } from "../theme";
import { toastError, toastSuccess } from "../toast";
import { SAMPLE_PENDING, sampleRecentToolCalls } from "./sample_data";

const noop = () => {};
const noopNavigate = (_view: ConsoleNavigationView) => {};

const ENROLLMENT_ID = "10000000-0000-4000-8000-000000000001";
const CONVERSATION_ID = "70000000-0000-4000-8000-000000000001";

function ConsoleScene({
  view,
  reconnect = false,
  conversationId = null,
  sessionFramesId = null,
}: {
  view: ConsoleView;
  reconnect?: boolean;
  conversationId?: string | null;
  sessionFramesId?: string | null;
}) {
  return (
    <HakuUiEmbed
      uiUrl="https://haku-ui.test/"
      launchAvailable
      view={view}
      agentEnrollmentId={view === "agentEnrollment" ? ENROLLMENT_ID : null}
      agentEnrollmentInitialChoice={reconnect ? "reconnect" : undefined}
      conversationId={conversationId}
      sessionFramesId={sessionFramesId}
      onNavigate={noopNavigate}
    />
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
  liveStatus: "live",
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

function IndicatorScene({ state }: { state: "current" | "syncing" | "error" }) {
  return (
    <div className="haku-console-shell">
      <ShellChrome
        {...chromeProps}
        liveStatus={state === "error" ? "offline" : "live"}
        syncError={state === "error" ? "Unauthorized" : null}
        syncing={state === "syncing"}
      />
      <main className="haku-shell-content" />
    </div>
  );
}

// The rail's session warning, which only appears inside the last few minutes of the session.
function SessionExpiringScene() {
  return (
    <div className="haku-console-shell">
      <ShellChrome {...chromeProps} sessionExpiresAt={new Date(Date.now() + 4 * 60_000)} sessionExpiringSoon />
      <main className="haku-shell-content" />
    </div>
  );
}

function OAuthSettingsResultScene({ status }: { status: "success" | "error" }) {
  useEffect(() => {
    if (status === "success") {
      toastSuccess("Connected to grocy-sf", "The MCP account is now available in Haku Console.");
    } else {
      toastError("Couldn't connect the MCP account", "The authorization request expired.");
    }
  }, [status]);
  return <ConsoleScene view="settings" />;
}

function sceneElement(scene: string) {
  switch (scene) {
    case "settings":
    case "settings-mobile":
    case "settings-agents":
    case "settings-notifications":
    case "settings-nodes":
    case "settings-system":
      return <ConsoleScene view="settings" />;
    case "claude-chat":
    case "claude-message-boundaries":
    case "claude-chat-mobile":
    case "claude-chat-overflow":
    case "claude-tool-use":
    case "claude-tool-use-mobile":
    case "claude-provisioning":
    case "claude-provisioning-mobile":
      return <ConsoleScene view="claudeChat" />;
    case "conversations":
    case "conversations-mobile":
      return <ConsoleScene view="conversations" />;
    case "conversation-detail":
    case "conversation-detail-mobile":
    case "conversation-bootstrap":
    case "conversation-bootstrap-mobile":
    case "conversation-narration-collapsed":
    case "conversation-prompt-refused":
      return <ConsoleScene view="conversations" conversationId={CONVERSATION_ID} />;
    case "session-frames":
    case "session-frames-mobile":
      return <ConsoleScene view="sessionFrames" sessionFramesId={CONVERSATION_ID} />;
    case "settings-oauth-success":
      return <OAuthSettingsResultScene status="success" />;
    case "settings-oauth-error":
      return <OAuthSettingsResultScene status="error" />;
    case "agent-enrollment":
    case "agent-enrollment-mobile":
      return <ConsoleScene view="agentEnrollment" />;
    case "agent-enrollment-reconnect":
      return <ConsoleScene view="agentEnrollment" reconnect />;
    case "history":
    case "history-auto-approved":
    case "history-paged":
      return <ConsoleScene view="toolCalls" />;
    case "sync-current":
      return <IndicatorScene state="current" />;
    case "sync-syncing":
      return <IndicatorScene state="syncing" />;
    case "sync-error":
      return <IndicatorScene state="error" />;
    case "session-expiring":
      return <SessionExpiringScene />;
    case "oauth-success":
    case "oauth-success-mobile":
      return (
        <OAuthResultView
          result={{
            status: "success",
            title: "Connected to Google Calendar",
            message: "The account is now available in Haku Console.",
          }}
          onClose={noop}
        />
      );
    case "oauth-error":
      return (
        <OAuthResultView
          result={{
            status: "error",
            title: "Couldn't connect the MCP account",
            message: "The authorization request expired or was superseded by a newer attempt.",
          }}
          onClose={noop}
        />
      );
    default:
      return <ConsoleScene view="embed" />;
  }
}

const scene = (window as unknown as { __SCENE__?: string }).__SCENE__ ?? "console";
const colorScheme = (window as unknown as { __COLOR_SCHEME__?: "light" | "dark" }).__COLOR_SCHEME__ ?? "light";
const container = document.getElementById("app");
if (!container) throw new Error("missing #app");
createRoot(container).render(
  <MantineProvider forceColorScheme={colorScheme} theme={hakuTheme}>
    <Notifications position="top-right" />
    {sceneElement(scene)}
  </MantineProvider>
);
