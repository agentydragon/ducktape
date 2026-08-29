import { afterEach, describe, expect, it, vi } from "vitest";

import { api, createConversation, type ChatLaunchOption, type Conversation } from "./client";

const selection = {
  agent_id: "00000000-0000-4000-8000-000000000002",
  agent_display_name: "public-coder-agent",
  runtime: "codex_app_server",
  runtime_display_name: "Codex",
} satisfies ChatLaunchOption;

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
        runtime: "codex_app_server",
      },
    });
  });
});
