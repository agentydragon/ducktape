// Screenshot harness: renders each console visual surface into #app with mocked data, one
// scene per page load (selected by `window.__SCENE__`). Bundled to an IIFE by
// esbuild.config.mjs and driven by render.mjs, which screenshots each scene to a PNG. A
// generator for eyeballing the visuals, not a pixel-diff gate — see frontend/AGENTS.md.
//
// `./mock_api.ts` is imported FIRST so its `fetch` stub is installed before client.ts (via
// tool_calls_page.tsx) captures `globalThis.fetch`; the history view then renders populated.
import "./mock_api.ts";

import { MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";

import { ShellDrawer, type ShellDrawerProps, type ShellDrawerTab } from "../console_panel.tsx";
import { hakuTheme } from "../theme.ts";
import { ToolCallsPage } from "../tool_calls_page.tsx";
import { SAMPLE_MCP, SAMPLE_PENDING, SAMPLE_RECENT } from "./sample_data.ts";

const noop = () => {};

function drawerProps(activeTab: ShellDrawerTab): ShellDrawerProps {
  return {
    opened: true,
    activeTab,
    onOpenTab: noop,
    onClose: noop,
    pendingApprovals: SAMPLE_PENDING,
    geolocationApprovals: [],
    selectedApprovalId: null,
    selectedRecentToolCallId: null,
    decidingApprovalIds: [],
    recentToolCalls: SAMPLE_RECENT,
    onSelectApproval: noop,
    onSelectRecentToolCall: noop,
    onApproveTool: noop,
    onDenyTool: noop,
    onApproveGeolocation: noop,
    onDenyGeolocation: noop,
    onDismissRecentToolCall: noop,
    onOpenToolCalls: noop,
    geoGranted: true,
    tracking: false,
    onWithdrawGeolocation: noop,
    mcpAuthStatuses: SAMPLE_MCP,
    onConnectMcp: noop,
    onDisconnectMcp: noop,
    onRefreshMcp: noop,
  };
}

function sceneElement(scene: string) {
  switch (scene) {
    case "drawer-access":
      return <ShellDrawer {...drawerProps("access")} />;
    case "drawer":
      return <ShellDrawer {...drawerProps("approvals")} />;
    default:
      return <ToolCallsPage onBack={noop} />;
  }
}

const scene = (window as unknown as { __SCENE__?: string }).__SCENE__ ?? "history";
const container = document.getElementById("app");
if (!container) throw new Error("missing #app");
createRoot(container).render(
  <MantineProvider defaultColorScheme="light" theme={hakuTheme}>
    {sceneElement(scene)}
  </MantineProvider>
);
