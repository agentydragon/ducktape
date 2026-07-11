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

import { ShellControls, ShellDrawer, type ShellDrawerProps } from "../console_panel.tsx";
import { SettingsPage } from "../settings_page.tsx";
import { hakuTheme } from "../theme.ts";
import { ToolActionLine } from "../tool_action_line.tsx";
import { ToolArgumentsField } from "../tool_arguments_field.tsx";
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
          {PREVIEW_SAMPLES.map(({ serverId, toolName, args }) => {
            const argumentsJson = JSON.stringify(args, null, 2);
            return (
              <div key={`${serverId}.${toolName}`} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <div>
                  <Text fw={700} size="sm" ff="monospace">
                    {serverId}.{toolName}
                  </Text>
                  {/* The action-description line each card renders in its header (or the raw
                      serverId.toolName fallback for the widget-less sample). */}
                  <ToolActionLine serverId={serverId} toolName={toolName} args={args} />
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, alignItems: "start" }}>
                  {(["compact", "detailed"] as const).map((variant) => (
                    <section className="haku-shell-card" key={variant}>
                      <Text size="xs" c="dimmed" fw={700} tt="uppercase" mb={6}>
                        {variant}
                      </Text>
                      <ToolArgumentsField
                        serverId={serverId}
                        toolName={toolName}
                        args={args}
                        argumentsJson={argumentsJson}
                        variant={variant}
                      />
                    </section>
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

const drawerProps: ShellDrawerProps = {
  opened: true,
  onClose: noop,
  pendingApprovals: SAMPLE_PENDING,
  geolocationApprovals: [],
  decidingApprovalIds: [],
  recentToolCalls: SAMPLE_RECENT,
  onApproveTool: noop,
  onDenyTool: noop,
  onApproveGeolocation: noop,
  onDenyGeolocation: noop,
  onDismissRecentToolCall: noop,
  onOpenToolCalls: noop,
  onOpenSettings: noop,
};

function sceneElement(scene: string) {
  switch (scene) {
    case "settings":
      // SettingsPage fetches /api/mcp/operator-auth on mount; mock_api.ts serves SAMPLE_MCP.
      return <SettingsPage onBack={noop} />;
    case "controls":
      // The shell chrome stack: hamburger (with a pending callout) plus the location pin
      // with its live indicator. render.mjs clicks the pin to open its popover.
      return (
        <ShellControls
          pendingCount={1}
          opened={false}
          onToggle={noop}
          geoGranted
          tracking
          onWithdrawGeolocation={noop}
        />
      );
    case "drawer":
      return <ShellDrawer {...drawerProps} />;
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
