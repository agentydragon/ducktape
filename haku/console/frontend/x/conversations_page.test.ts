import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ConversationPage, ConversationSummary } from "../client";
import { ConversationsPage } from "./conversations_page";

const mocks = vi.hoisted(() => ({
  fetchConfig: vi.fn(),
  fetchConversations: vi.fn(),
  onEvent: null as ((event: { event_type: string; conversation_id?: string }) => void) | null,
}));

vi.mock("../client", () => ({
  closeSession: vi.fn(),
  createConversation: vi.fn(),
  displayableError: (error: unknown) => String(error),
  fetchConfig: mocks.fetchConfig,
  fetchConversations: mocks.fetchConversations,
}));

vi.mock("../console_events", () => ({
  changedConversationId: (event: { event_type: string; conversation_id?: string }) =>
    event.event_type === "conversation_changed" ? (event.conversation_id ?? null) : null,
  useConsoleEvents: (onEvent: (event: { event_type: string; conversation_id?: string }) => void) => {
    mocks.onEvent = onEvent;
    return "live";
  },
}));

vi.mock("../routing", () => ({
  CONVERSATIONS_PATH: "/_console/conversations",
  conversationPath: (conversationId: string) => `/_console/conversations/${conversationId}`,
  navigateToConsolePath: vi.fn(),
  sessionFramesPath: (sessionId: string) => `/_console/sessions/${sessionId}/frames`,
}));

vi.mock("./conversation_follow", () => ({
  useFollowedConversation: () => ({ conversation: null, status: "offline", error: null }),
}));
vi.mock("./conversation_composer", () => ({ ConversationComposer: () => null }));
vi.mock("./markdown", () => ({ Markdown: () => null }));
vi.mock("./sandbox_provisioning", () => ({ SandboxProvisioning: () => null }));
vi.mock("./tool_call", () => ({ ToolCallView: () => null }));
vi.mock("../agent_names", () => ({ AgentName: () => null }));
vi.mock("../icons", () => ({ InfoCircleIcon: () => null, NewConversationIcon: () => null }));

function summary(conversationId: string, activity: string): ConversationSummary {
  return {
    conversation_id: conversationId,
    agent_id: null,
    created_at: activity,
    last_activity_at: activity,
    last_session_status: null,
    item_count: 1,
    attachments: [],
    live_session: null,
    preview: null,
    harness_kind: "codex_app_server",
  } as ConversationSummary;
}

function page(conversations: ConversationSummary[], nextCursor: null = null): ConversationPage {
  return { conversations, next_cursor: nextCursor } as ConversationPage;
}

describe("ConversationsPage live list refresh", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot> | null = null;

  afterEach(() => {
    if (root) act(() => root?.unmount());
    mocks.fetchConfig.mockReset();
    mocks.fetchConversations.mockReset();
    mocks.onEvent = null;
  });

  it.fails("reproduces scroll loss when an SSE refresh reshuffles conversations", async () => {
    mocks.fetchConfig.mockResolvedValue({ launch_options: [] });
    mocks.fetchConversations
      .mockResolvedValueOnce(page([summary("a", "2026-08-31T00:03:00Z"), summary("b", "2026-08-31T00:02:00Z")]))
      .mockResolvedValueOnce(
        page([
          summary("b", "2026-08-31T00:05:00Z"),
          summary("a", "2026-08-31T00:03:00Z"),
          summary("c", "2026-08-31T00:01:00Z"),
        ])
      );
    container = document.createElement("div");
    const appRoot = createRoot(container);
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    root = appRoot;

    act(() => appRoot.render(createElement(ConversationsPage, { conversationId: null })));
    expect(mocks.onEvent).not.toBeNull();
    act(() => mocks.onEvent?.({ event_type: "sync" }));
    await vi.waitFor(() => expect(mocks.fetchConversations).toHaveBeenCalledTimes(1));

    await vi.waitFor(() => expect(container.querySelectorAll("button.haku-conversation-list-item")).toHaveLength(2));

    const scroll = container.querySelector<HTMLDivElement>(".haku-page-scroll");
    if (!scroll) throw new Error("conversation list scroll container was not rendered");
    Object.defineProperty(scroll, "scrollHeight", { configurable: true, value: 1000 });
    Object.defineProperty(scroll, "clientHeight", { configurable: true, value: 320 });
    scroll.scrollTop = 240;

    act(() => mocks.onEvent?.({ event_type: "conversation_changed", conversation_id: "b" }));
    await vi.waitFor(() => expect(container.querySelectorAll("button.haku-conversation-list-item")).toHaveLength(3));

    expect(container.querySelector(".haku-page-scroll")).toBe(scroll);
    expect(scroll.scrollTop).toBe(240);
  });
});
