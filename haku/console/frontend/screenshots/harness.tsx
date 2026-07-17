// Screenshot harness for the console's full-page visual surfaces (the history view, shell
// chrome, and settings panel). Renders the surface selected by `window.__SCENE__` into #app with
// mocked data, one scene and color scheme per page load. Bundled to an IIFE by
// esbuild.config.mjs and driven by render.mjs, which screenshots each combination to a PNG. A
// generator for eyeballing the visuals, not a pixel-diff gate — see frontend/AGENTS.md.
//
// Per-tool preview cards have their own harness in tool_rendering/screenshot/ (one `:previews`
// target per server under tool_rendering/<server>/).
//
// `./mock_api.ts` is imported FIRST so its `fetch` stub is installed before client.ts (via
// tool_calls_page.tsx) captures `globalThis.fetch`; the history view then renders populated.
import "./mock_api.ts";

import { MantineProvider } from "@mantine/core";
import { useState } from "react";
import { createRoot } from "react-dom/client";

import { SettingsPanel } from "../settings_panel.tsx";
import { ShellChrome, type ShellChromeProps } from "../shell_chrome.tsx";
import { hakuTheme } from "../theme.ts";
import { ToolCallsPage } from "../tool_calls_page.tsx";
import { SAMPLE_PENDING, sampleRecentToolCalls } from "./sample_data.ts";

const noop = () => {};

const chromeProps: Omit<ShellChromeProps, "approvalsOpen" | "onApprovalsOpenChange" | "recentToolCalls"> = {
  pendingApprovals: SAMPLE_PENDING,
  geolocationApprovals: [],
  screenshotApprovals: [],
  decidingApprovalIds: [],
  onApproveTool: noop,
  onDenyTool: noop,
  onApproveGeolocation: noop,
  onDenyGeolocation: noop,
  onApproveScreenshot: noop,
  onDenyScreenshot: noop,
  onDismissRecentToolCall: noop,
  onOpenToolCalls: noop,
  liveStatus: "offline",
  syncError: false,
  geoGranted: true,
  tracking: true,
  onWithdrawGeolocation: noop,
  screenshotGranted: true,
  sharingScreen: true,
  onWithdrawScreenshot: noop,
};

function ShellChromeScene() {
  const [approvalsOpen, setApprovalsOpen] = useState(true);
  return (
    <ShellChrome
      {...chromeProps}
      approvalsOpen={approvalsOpen}
      onApprovalsOpenChange={setApprovalsOpen}
      recentToolCalls={sampleRecentToolCalls(Date.now())}
    />
  );
}

function sceneElement(scene: string) {
  switch (scene) {
    case "settings":
      // The settings panel (a chrome surface); fetches /api/mcp/operator-auth on mount —
      // mock_api.ts serves SAMPLE_MCP. Render it inside its real shell column
      // (.haku-shell-panels, which owns the panel width) so the shot tracks the true layout
      // instead of a hardcoded number; #shot is render.mjs's element-screenshot target and
      // shrink-wraps that column, with padding so the shot includes the card shadows.
      return (
        <div id="shot" style={{ display: "inline-block", padding: 24 }}>
          <div className="haku-shell-panels">
            <SettingsPanel />
          </div>
        </div>
      );
    case "chrome":
      // The whole shell chrome: the toggle-button toolbar (offline warning + location pin +
      // settings + approvals) over the panel column. Approvals starts open; render.mjs switches
      // between the mutually exclusive panel tabs.
      return <ShellChromeScene />;
    // The history page; render.mjs expands its first rows into their detailed state (opening the
    // Metadata disclosure) with a click sequence, so one shot shows both compact and detailed rows.
    default:
      return <ToolCallsPage onBack={noop} />;
  }
}

const scene = (window as unknown as { __SCENE__?: string }).__SCENE__ ?? "history";
const colorScheme = (window as unknown as { __COLOR_SCHEME__?: "light" | "dark" }).__COLOR_SCHEME__ ?? "light";
const container = document.getElementById("app");
if (!container) throw new Error("missing #app");
createRoot(container).render(
  <MantineProvider forceColorScheme={colorScheme} theme={hakuTheme}>
    {sceneElement(scene)}
  </MantineProvider>
);
