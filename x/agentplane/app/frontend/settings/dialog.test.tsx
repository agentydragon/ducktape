// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { Settings, type SettingsTab } from "./dialog";

// Settings only wires the Modal/Tabs shell; each panel's own behavior is covered by its own test.
vi.mock("./connections", () => ({ Connections: () => <div>oauth-clients-panel</div> }));
vi.mock("./mcp_servers", () => ({ McpServers: () => <div>mcp-servers-panel</div> }));
vi.mock("./push", () => ({ PushSettings: () => <div>notifications-panel</div> }));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];
afterEach(async () => {
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
});

// Mantine's Modal portals its content onto document.body rather than the React root's own DOM
// node, so every query below reads from the document, not a container returned by render().
async function render(
  tab: SettingsTab,
  overrides: { opened?: boolean; onTabChange?: (tab: SettingsTab) => void; onClose?: () => void } = {}
): Promise<void> {
  const { opened = true, onTabChange = vi.fn(), onClose = vi.fn() } = overrides;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () =>
    root.render(
      <MantineProvider>
        <Settings opened={opened} tab={tab} onTabChange={onTabChange} onClose={onClose} />
      </MantineProvider>
    )
  );
}

function visiblePanelText(): string | null | undefined {
  return [...document.querySelectorAll<HTMLElement>('[role="tabpanel"]')].find(
    (panel) => panel.style.display !== "none"
  )?.textContent;
}

function tab(name: string): HTMLButtonElement {
  const found = [...document.querySelectorAll<HTMLButtonElement>('button[role="tab"]')].find(
    (button) => button.textContent === name
  );
  if (!found) throw new Error(`Missing ${name} tab`);
  return found;
}

it("shows only the selected tab's panel while open", async () => {
  await render("mcp-servers");
  expect(visiblePanelText()).toContain("mcp-servers-panel");
});

it("reports a clicked tab through onTabChange rather than switching itself", async () => {
  const onTabChange = vi.fn();
  await render("oauth-clients", { onTabChange });
  await act(async () => tab("Notifications").click());
  expect(onTabChange).toHaveBeenCalledWith("notifications");
  // The `tab` prop hasn't changed, so the originally selected panel is still the one shown.
  expect(visiblePanelText()).toContain("oauth-clients-panel");
});

it("renders nothing while closed", async () => {
  await render("oauth-clients", { opened: false });
  expect(document.querySelector('[role="dialog"]')).toBeNull();
});
