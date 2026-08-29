import { describe, expect, it } from "vitest";

import type { ChatLaunchOption } from "./client";
import {
  conversationLaunchOptions,
  initialLaunchKey,
  launchKey,
  shouldShowLaunchSelector,
} from "./conversation_launch";

const haku = {
  agent_id: "00000000-0000-4000-8000-000000000001",
  agent_display_name: "Haku",
  runtime: "claude_code",
  runtime_display_name: "Claude Code",
} satisfies ChatLaunchOption;

const coder = {
  agent_id: "00000000-0000-4000-8000-000000000002",
  agent_display_name: "public-coder-agent",
  runtime: "codex_app_server",
  runtime_display_name: "Codex",
} satisfies ChatLaunchOption;

describe("conversation launch choices", () => {
  it("tolerates an older API replica with no launch catalog", () => {
    expect(conversationLaunchOptions({})).toEqual([]);
    expect(initialLaunchKey(conversationLaunchOptions({}))).toBeNull();
  });

  it("selects the sole launch choice without inventing a default", () => {
    expect(initialLaunchKey([coder])).toBe(launchKey(coder));
  });

  it("requires an explicit choice when multiple launches are available", () => {
    expect(initialLaunchKey([coder, haku])).toBeNull();
  });

  it("only needs a selector when multiple launches are available", () => {
    expect(shouldShowLaunchSelector([])).toBe(false);
    expect(shouldShowLaunchSelector([coder])).toBe(false);
    expect(shouldShowLaunchSelector([haku, coder])).toBe(true);
  });
});
