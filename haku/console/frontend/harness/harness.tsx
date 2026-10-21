// Full-page screenshot harness for Haku Console. The shell is rendered with mocked API data;
// the test's request fence answers the real iframe request with a striped Haku UI document.
import "./mock_api";

import { MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { useEffect } from "react";
import { createRoot } from "react-dom/client";

import { HakuUiEmbed } from "../haku_ui_embed";
import type { ConsoleNavigationView, ConsoleView } from "../routing";
import { ShellChrome, type ShellChromeProps } from "../shell_chrome";
import { hakuTheme } from "../theme";

const noop = () => {};
const noopNavigate = (_view: ConsoleNavigationView) => {};
const UI_ORIGIN = "https://haku-ui.test";

function ConsoleFixture({ view }: { view: ConsoleView }) {
  return <HakuUiEmbed uiUrl={`${UI_ORIGIN}/`} launchAvailable view={view} onNavigate={noopNavigate} />;
}

const chromeProps: ShellChromeProps = {
  view: "embed",
  onNavigate: noopNavigate,
  capabilityRequestsOpen: false,
  onCapabilityRequestsOpenChange: noop,
  geolocationApprovals: [],
  screenshotApprovals: [],
  decidingCapabilityRequestIds: [],
  onApproveGeolocation: noop,
  onDenyGeolocation: noop,
  onApproveScreenshot: noop,
  onDenyScreenshot: noop,
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

function CapabilityRequestFixture() {
  useEffect(() => {
    const timeout = window.setTimeout(() => {
      window.dispatchEvent(
        new MessageEvent("message", {
          origin: UI_ORIGIN,
          data: { type: "requestGeolocation", id: "visual-geolocation", options: { enableHighAccuracy: true } },
        })
      );
    }, 0);
    return () => window.clearTimeout(timeout);
  }, []);
  return <ConsoleFixture view="embed" />;
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
    case "settings":
      return <ConsoleFixture view="settings" />;
    case "capability-request":
      return <CapabilityRequestFixture />;
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
