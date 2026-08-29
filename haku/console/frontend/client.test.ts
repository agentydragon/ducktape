import { afterEach, describe, expect, it, vi } from "vitest";

import { api, createConversation, type LaunchOption, type Conversation } from "./client";

const selection = {
  agent_id: "00000000-0000-4000-8000-000000000002",
  agent_display_name: "public-coder-agent",
  harness_kind: "codex_app_server",
  harness_display_name: "Codex",
} satisfies LaunchOption;

const conversation = {
  conversation_id: "00000000-0000-4000-8000-000000000099",
} as Conversation;

function mockConversationPost() {
  return vi.spyOn(api, "POST").mockResolvedValue({ data: conversation } as never);
}

afterEach(() => vi.restoreAllMocks());

describe("createConversation", () => {
  it("submits the explicit Agent/harness pair without a profile selector", async () => {
    const post = mockConversationPost();

    expect(await createConversation(selection)).toBe(conversation);

    expect(post).toHaveBeenCalledWith("/api/conversations", {
      body: {
        agent_id: selection.agent_id,
        harness_kind: "codex_app_server",
      },
    });
  });
});
