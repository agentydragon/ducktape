// @vitest-environment happy-dom
import { focusManager, onlineManager, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { afterEach, expect, it, vi } from "vitest";

import { listAgents, fetchDeploymentInfo, updateAgentAccessProfile } from "./client";
import { createConsoleQueryClient } from "./query";
import { SettingsPanel } from "./settings_panel";
import type { AgentListResponse, AgentView } from "./schema";
import { deferred } from "../../../util/frontend/testing/deferred";
import { cleanupMounted, renderWithMantine } from "../../../util/frontend/testing/react";

vi.mock(import("./client"), async (original) => ({
  ...(await original()),
  listAgents: vi.fn(),
  fetchDeploymentInfo: vi.fn(),
  updateAgentAccessProfile: vi.fn(),
}));
vi.mock("./toast", () => ({ toastSuccess: vi.fn(), toastError: vi.fn() }));

const agent: AgentView = {
  agent_id: "40000000-0000-4000-8000-000000000004",
  display_name: "Claude Desktop",
  status: "active",
  credential_kind: "oauth",
  credential_status: "active",
  created_at: "2026-07-18T12:00:00Z",
  activated_at: "2026-07-18T12:05:00Z",
  last_seen_at: "2026-07-20T19:30:00Z",
  access_profile_id: "haku_v1",
};
const catalog: AgentListResponse = { agents: [agent], access_profiles: ["haku_v1", "manual_review"] };
const clients: ReturnType<typeof createConsoleQueryClient>[] = [];

afterEach(async () => {
  await cleanupMounted();
  for (const client of clients.splice(0)) client.clear();
  focusManager.setFocused(undefined);
  onlineManager.setOnline(true);
  vi.useRealTimers();
  vi.resetAllMocks();
});

async function mountSettings() {
  window.history.replaceState(null, "", "/_console/settings");
  const client = createConsoleQueryClient();
  clients.push(client);
  const container = await renderWithMantine(
    <QueryClientProvider client={client}>
      <SettingsPanel />
    </QueryClientProvider>
  );
  return { container, client };
}
function button(container: HTMLElement, text: string): HTMLElement {
  const element = [...container.querySelectorAll<HTMLElement>("button")].find((node) => node.textContent === text);
  if (!element) throw new Error(`Missing ${text} button`);
  return element;
}
const click = (element: HTMLElement): Promise<void> => act(async () => element.click());

it("polls only the active tab, retains failed refresh data, and cancels an inactive read", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const pending = deferred<AgentListResponse>();
  let oldSignal: AbortSignal | undefined;
  vi.mocked(listAgents)
    .mockResolvedValueOnce(catalog)
    .mockRejectedValueOnce(new Error("offline"))
    .mockImplementationOnce((signal) => {
      oldSignal = signal;
      return pending.promise;
    });
  const image = { source_commit: "abcdef", source_commit_url: null, image_tag: null };
  vi.mocked(fetchDeploymentInfo).mockResolvedValue({ server: image, frontend: image });
  const { container, client } = await mountSettings();
  await vi.waitFor(() => expect(container.textContent).toContain("Claude Desktop"));
  expect(fetchDeploymentInfo).not.toHaveBeenCalled();
  focusManager.setFocused(false);
  await act(async () => vi.advanceTimersByTimeAsync(30_000));
  await vi.waitFor(() => expect(container.textContent).toContain("Failed to refresh Agents: offline"));
  expect(container.textContent).toContain("Claude Desktop");
  expect(listAgents).toHaveBeenCalledTimes(2);
  await click(button(container, "Refresh"));
  expect(listAgents).toHaveBeenCalledTimes(3);
  await click(button(container, "System"));
  await vi.waitFor(() => expect(container.textContent).toContain("In sync"));
  expect(oldSignal?.aborted).toBe(true);
  await act(async () => vi.advanceTimersByTimeAsync(30_000));
  expect(listAgents).toHaveBeenCalledTimes(3);
  expect(fetchDeploymentInfo).toHaveBeenCalledTimes(2);
  await act(async () => pending.resolve({ ...catalog, agents: [{ ...agent, display_name: "Late catalog" }] }));
  expect(client.getQueryData(["settings", "agents"])).toEqual(catalog);
});

it("keeps a successful profile edit when an older poll settles", async () => {
  const pending = deferred<AgentListResponse>();
  let pollSignal: AbortSignal | undefined;
  vi.mocked(listAgents)
    .mockResolvedValueOnce(catalog)
    .mockImplementationOnce((signal) => {
      pollSignal = signal;
      return pending.promise;
    });
  vi.mocked(updateAgentAccessProfile).mockResolvedValue({ ...agent, access_profile_id: "manual_review" });
  const { container, client } = await mountSettings();
  await vi.waitFor(() => expect(container.textContent).toContain("Claude Desktop"));
  await click(button(container, "Refresh"));
  onlineManager.setOnline(false);
  const select = container.querySelector<HTMLInputElement>('input[aria-label="Access profile for Claude Desktop"]')!;
  await click(select);
  const option = [...document.querySelectorAll<HTMLElement>('[role="option"]')].find(
    (node) => node.textContent === "manual review"
  );
  if (!option) throw new Error("Missing manual review option");
  await click(option);
  await vi.waitFor(() => expect(select.value).toBe("manual review"));
  expect(pollSignal?.aborted).toBe(true);
  expect(updateAgentAccessProfile).toHaveBeenCalledExactlyOnceWith(agent.agent_id, "manual_review");
  await act(async () => pending.resolve(catalog));
  expect(select.value).toBe("manual review");
  expect(client.getQueryData<AgentListResponse>(["settings", "agents"])?.agents[0]?.access_profile_id).toBe(
    "manual_review"
  );
});
