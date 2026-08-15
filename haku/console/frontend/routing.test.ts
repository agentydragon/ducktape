import { beforeEach, describe, expect, it } from "vitest";

import {
  AGENT_ENROLLMENT_PATH_PREFIX,
  agentEnrollmentIdForPathname,
  CLAUDE_CHAT_PATH,
  CONVERSATIONS_PATH,
  CONSOLE_ROOT_PATH,
  HOME_PATH,
  OAUTH_RESULT_PATH_PREFIX,
  oauthResultIdForPathname,
  rememberedEmbedPath,
  rememberEmbedPath,
  SETTINGS_PATH,
  TOOL_CALLS_PATH,
  toolCallIdForPathname,
  conversationIdForPathname,
  viewForPathname,
} from "./routing";

describe("viewForPathname", () => {
  it("reserves only the _console namespace for trusted pages", () => {
    expect(viewForPathname(SETTINGS_PATH)).toBe("settings");
    expect(viewForPathname(`${AGENT_ENROLLMENT_PATH_PREFIX}/10000000-0000-4000-8000-000000000001`)).toBe(
      "agentEnrollment"
    );
    expect(viewForPathname(TOOL_CALLS_PATH)).toBe("toolCalls");
    expect(viewForPathname(CLAUDE_CHAT_PATH)).toBe("claudeChat");
    expect(viewForPathname(CONVERSATIONS_PATH)).toBe("conversations");
    expect(viewForPathname(`${CONVERSATIONS_PATH}/10000000-0000-4000-8000-000000000001`)).toBe("conversations");
    expect(viewForPathname(`${OAUTH_RESULT_PATH_PREFIX}/8de5eb42-a3ce-4c83-9b13-59678c399ba3`)).toBe("oauthResult");
    expect(viewForPathname(`${CONSOLE_ROOT_PATH}/unknown`)).toBe("notFound");
    expect(viewForPathname(CONSOLE_ROOT_PATH)).toBe("embed");
  });

  it("accepts only canonical UUIDv4 conversation routes", () => {
    const id = "10000000-0000-4000-8000-000000000001";
    expect(conversationIdForPathname(`${CONVERSATIONS_PATH}/${id}`)).toBe(id);
    expect(conversationIdForPathname(`${CONVERSATIONS_PATH}/not-a-conversation`)).toBeNull();
    expect(viewForPathname(`${CONVERSATIONS_PATH}/not-a-conversation`)).toBe("notFound");
  });

  it("resolves a deep-linked tool call to the shell, which opens the drawer on that call", () => {
    const id = "tc_0123456789abcdef01234567";
    expect(toolCallIdForPathname(`${TOOL_CALLS_PATH}/${id}`)).toBe(id);
    // Not the history page: a pending call is decided in the approvals drawer, which floats over
    // the ordinary embed view.
    expect(viewForPathname(`${TOOL_CALLS_PATH}/${id}`)).toBe("embed");
    // Tool call ids are `tc_` + 24 hex, not UUIDs like the other id-bearing console routes.
    expect(toolCallIdForPathname(`${TOOL_CALLS_PATH}/8de5eb42-a3ce-4c83-9b13-59678c399ba3`)).toBeNull();
    expect(toolCallIdForPathname(`${TOOL_CALLS_PATH}/tc_short`)).toBeNull();
    expect(viewForPathname(`${TOOL_CALLS_PATH}/tc_short`)).toBe("notFound");
  });

  it("accepts only canonical UUIDv4 OAuth result routes", () => {
    const id = "8de5eb42-a3ce-4c83-9b13-59678c399ba3";
    expect(oauthResultIdForPathname(`${OAUTH_RESULT_PATH_PREFIX}/${id}`)).toBe(id);
    expect(oauthResultIdForPathname(`${OAUTH_RESULT_PATH_PREFIX}/not-a-result`)).toBeNull();
    expect(viewForPathname(`${OAUTH_RESULT_PATH_PREFIX}/not-a-result`)).toBe("notFound");
  });

  it("accepts only canonical UUIDv4 Agent enrollment routes", () => {
    const id = "10000000-0000-4000-8000-000000000001";
    expect(agentEnrollmentIdForPathname(`${AGENT_ENROLLMENT_PATH_PREFIX}/${id}`)).toBe(id);
    expect(agentEnrollmentIdForPathname(`${AGENT_ENROLLMENT_PATH_PREFIX}/not-an-interaction`)).toBeNull();
    expect(viewForPathname(`${AGENT_ENROLLMENT_PATH_PREFIX}/not-an-interaction`)).toBe("notFound");
  });

  it("returns every non-console path, including the old history path, to haku-ui", () => {
    expect(viewForPathname(HOME_PATH)).toBe("embed");
    expect(viewForPathname("/tool-calls")).toBe("embed");
    expect(viewForPathname("/garden/a.md")).toBe("embed");
  });
});

describe("rememberEmbedPath", () => {
  beforeEach(() => sessionStorage.clear());

  it("remembers haku-ui paths and ignores console paths", () => {
    rememberEmbedPath("/garden/a.md");
    expect(rememberedEmbedPath()).toBe("/garden/a.md");
    rememberEmbedPath(SETTINGS_PATH);
    expect(rememberedEmbedPath()).toBe("/garden/a.md");
  });
});
