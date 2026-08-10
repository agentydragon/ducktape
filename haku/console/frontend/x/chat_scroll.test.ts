import { describe, expect, it } from "vitest";

import { isNearChatBottom } from "./chat_scroll";

describe("isNearChatBottom", () => {
  it("keeps following when the viewport is at or near the bottom", () => {
    expect(isNearChatBottom({ scrollHeight: 1000, scrollTop: 500, clientHeight: 500 })).toBe(true);
    expect(isNearChatBottom({ scrollHeight: 1000, scrollTop: 455, clientHeight: 500 })).toBe(true);
  });

  it("stops following after the operator scrolls up", () => {
    expect(isNearChatBottom({ scrollHeight: 1000, scrollTop: 400, clientHeight: 500 })).toBe(false);
  });
});
