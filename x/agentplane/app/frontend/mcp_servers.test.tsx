// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import type { McpLinkageService, McpLinkageView } from "./client";
import { McpServers } from "./mcp_servers";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];
afterEach(async () => {
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
});

async function render(
  list: McpLinkageService["list"],
  overrides: Partial<McpLinkageService> = {}
): Promise<HTMLDivElement> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  const service: McpLinkageService = { list, status: vi.fn(), start: vi.fn(), disconnect: vi.fn(), ...overrides };
  await act(async () =>
    root.render(
      <MantineProvider>
        <McpServers service={service} />
      </MantineProvider>
    )
  );
  return container;
}

function refresh(container: HTMLElement): HTMLButtonElement {
  const button = [...container.querySelectorAll("button")].find((node) => node.textContent === "Refresh");
  if (!button) throw new Error("Missing Refresh");
  return button;
}

function pendingList(): { promise: Promise<McpLinkageView[]>; resolve: (rows: McpLinkageView[]) => void } {
  let resolve!: (rows: McpLinkageView[]) => void;
  const promise = new Promise<McpLinkageView[]>((accept) => {
    resolve = accept;
  });
  return { promise, resolve };
}

it.each(["Reconnect", "Disconnect"])("spins only the clicked %s button until the operation fails", async (label) => {
  const rows: McpLinkageView[] = ["github", "second-server"].map((server_id) => ({
    server_id,
    provider: "github",
    server_url: "https://mcp.example.test",
    status: "linked",
    revision: 1,
    scopes: [],
    expires_at: null,
    linked_at: null,
    linked_by: null,
  }));
  let reject!: (error: Error) => void;
  const pending = new Promise<never>((_, fail) => {
    reject = fail;
  });
  const start = vi.fn<McpLinkageService["start"]>().mockReturnValue(pending);
  const disconnect = vi.fn<McpLinkageService["disconnect"]>().mockReturnValue(pending);
  const container = await render(async () => rows, { start, disconnect });
  const buttons = [...container.querySelectorAll("button")];
  const clicked = buttons.find((button) => button.textContent === label);
  if (!clicked) throw new Error(`Missing ${label}`);
  await act(async () => clicked.click());
  expect(container.querySelectorAll("button[data-loading]")).toHaveLength(1);
  expect(clicked.hasAttribute("data-loading")).toBe(true);
  expect(refresh(container).hasAttribute("data-loading")).toBe(false);
  expect(buttons.every((button) => button.disabled)).toBe(true);
  if (label === "Reconnect") expect(start).toHaveBeenCalledWith("github", []);
  else expect(disconnect).toHaveBeenCalledWith("github");

  await act(async () => reject(new Error("Operation failed")));
  expect(container.querySelectorAll("button[data-loading]")).toHaveLength(0);
  expect(buttons.every((button) => !button.disabled)).toBe(true);
  expect(container.textContent).toContain("Operation failed");
});

it("does not claim an empty inventory until the pending request succeeds", async () => {
  const response = pendingList();
  const container = await render(() => response.promise);
  expect(container.querySelector('[role="status"]')?.textContent).toContain("Loading MCP servers");
  expect(container.textContent).not.toContain("No OAuth-capable MCP servers");
  expect(refresh(container).disabled).toBe(true);

  await act(async () => response.resolve([]));
  expect(container.querySelector('[role="status"]')).toBeNull();
  expect(container.textContent).toContain("No OAuth-capable MCP servers");
  expect(refresh(container).disabled).toBe(false);
});

it("shows a failed load as an error, then clears it while retrying", async () => {
  const retry = pendingList();
  const list = vi.fn<McpLinkageService["list"]>().mockRejectedValueOnce(new Error("Service unavailable"));
  list.mockImplementationOnce(() => retry.promise);
  const container = await render(list);
  expect(container.textContent).toContain("Service unavailable");
  expect(container.textContent).not.toContain("No OAuth-capable MCP servers");
  expect(container.querySelector('[role="status"]')).toBeNull();

  await act(async () => refresh(container).click());
  expect(container.textContent).not.toContain("Service unavailable");
  expect(container.textContent).not.toContain("No OAuth-capable MCP servers");
  expect(container.querySelector('[role="status"]')).not.toBeNull();

  await act(async () => retry.resolve([]));
  expect(container.textContent).toContain("No OAuth-capable MCP servers");
  expect(container.querySelector('[role="status"]')).toBeNull();
});

it("keeps the last inventory visible while refreshing", async () => {
  const response = pendingList();
  const list = vi.fn<McpLinkageService["list"]>().mockResolvedValueOnce([
    {
      server_id: "example-mcp",
      provider: "github",
      server_url: "https://mcp.example.test",
      status: "unlinked",
      revision: 0,
      scopes: [],
      expires_at: null,
      linked_at: null,
      linked_by: null,
    },
  ]);
  list.mockImplementationOnce(() => response.promise);
  const container = await render(list);
  expect(container.textContent).toContain("example-mcp");

  await act(async () => refresh(container).click());
  expect(container.textContent).toContain("example-mcp");
  expect(container.querySelector('[role="status"]')).not.toBeNull();
  expect(container.textContent).not.toContain("No OAuth-capable MCP servers");

  await act(async () => response.resolve([]));
  expect(container.textContent).not.toContain("example-mcp");
  expect(container.textContent).toContain("No OAuth-capable MCP servers");
});
