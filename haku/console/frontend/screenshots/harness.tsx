// Screenshot harness: renders each console visual surface into #app with mocked data, one
// scene and color scheme per page load (selected by `window.__SCENE__` and
// `window.__COLOR_SCHEME__`). Bundled to an IIFE by esbuild.config.mjs and driven by
// render.mjs, which screenshots each combination to a PNG. A
// generator for eyeballing the visuals, not a pixel-diff gate — see frontend/AGENTS.md.
//
// `./mock_api.ts` is imported FIRST so its `fetch` stub is installed before client.ts (via
// tool_calls_page.tsx) captures `globalThis.fetch`; the history view then renders populated.
import "./mock_api.ts";

import { MantineProvider, Text } from "@mantine/core";
import { createRoot } from "react-dom/client";

import { ShellChrome, type ShellChromeProps } from "../console_panel.tsx";
import { SettingsPanel } from "../settings_page.tsx";
import { hakuTheme } from "../theme.ts";
import { approvalDisplayFields } from "../approval_state.ts";
import { ToolCallCard } from "../tool_call_card.tsx";
import { ToolCallsPage } from "../tool_calls_page.tsx";
import { PREVIEW_SAMPLES, SAMPLE_PENDING, SAMPLE_RECENT } from "./sample_data.ts";

const noop = () => {};

// Gallery of every implemented tool-call preview, each rendered in both variants side by
// side, so a glance covers the whole widget surface (see PREVIEW_SAMPLES / frontend/AGENTS.md).
function PreviewGallery() {
  return (
    <div className="haku-page">
      <div className="haku-page-scroll">
        <div
          style={{ maxWidth: 1000, margin: "0 auto", padding: 24, display: "flex", flexDirection: "column", gap: 28 }}
        >
          {PREVIEW_SAMPLES.map(({ title, serverId, toolName, args }, index) => {
            const fields = approvalDisplayFields({
              tool_call_id: `preview_${index}`,
              server_id: serverId,
              tool_name: toolName,
              caller_principal: "haku-agent-api-token",
              status: "pending_approval",
              created_at: "2026-07-11T12:00:00Z",
              updated_at: "2026-07-11T12:00:00Z",
              arguments: args,
              rationale: "Sample rationale for the operator.",
              title,
              result: null,
              error: null,
              denial_reason: null,
            });
            return (
              <div key={`${serverId}.${toolName}`} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, alignItems: "start" }}>
                  {(["compact", "detailed"] as const).map((variant) => (
                    <ToolCallCard
                      key={variant}
                      fields={fields}
                      args={args}
                      variant={variant}
                      onVariantChange={noop}
                      status={{ label: "Pending", color: "yellow" }}
                    />
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

const chromeProps: ShellChromeProps = {
  approvalsOpen: true,
  onApprovalsOpenChange: noop,
  pendingApprovals: SAMPLE_PENDING,
  geolocationApprovals: [],
  screenshotApprovals: [],
  decidingApprovalIds: [],
  recentToolCalls: SAMPLE_RECENT,
  onApproveTool: noop,
  onDenyTool: noop,
  onApproveGeolocation: noop,
  onDenyGeolocation: noop,
  onApproveScreenshot: noop,
  onDenyScreenshot: noop,
  onDismissRecentToolCall: noop,
  onOpenToolCalls: noop,
  liveStatus: "offline",
  geoGranted: true,
  tracking: true,
  onWithdrawGeolocation: noop,
  screenshotGranted: true,
  sharingScreen: true,
  onWithdrawScreenshot: noop,
};

function sceneElement(scene: string) {
  switch (scene) {
    case "settings":
      // The settings drawer panel (a chrome surface); fetches /api/mcp/operator-auth on mount —
      // mock_api.ts serves SAMPLE_MCP. Boxed to a panel-ish width for the shot.
      return (
        <div style={{ maxWidth: 520, margin: 16 }}>
          <SettingsPanel />
        </div>
      );
    case "chrome":
      // The whole shell chrome: the toggle-button toolbar (offline warning + location pin +
      // settings + approvals) over the panel column. Approvals starts open; render.mjs clicks
      // the live and location buttons so several panels show stacked by Y (the point of the
      // layout).
      return <ShellChrome {...chromeProps} />;
    case "previews":
      return <PreviewGallery />;
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
