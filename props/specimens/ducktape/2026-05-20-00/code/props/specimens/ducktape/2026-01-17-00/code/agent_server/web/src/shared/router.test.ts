import { describe, it, expect, beforeEach } from "vitest";

import { getAgentIdFromUrl, setAgentId, currentAgentId } from "./router";

describe("router helpers", () => {
  beforeEach(() => {
    // Reset path to root on the same origin to satisfy jsdom security model
    history.replaceState({}, "", "/");
  });

  it("getAgentIdFromUrl returns null when missing", () => {
    expect(getAgentIdFromUrl()).toBeNull();
  });

  it("setAgentId updates URL and store", async () => {
    setAgentId("abc");
    expect(new URL(window.location.href).searchParams.get("agent_id")).toBe("abc");
    let val: string | null = null;
    const unsub = currentAgentId.subscribe((v) => {
      val = v;
    });
    expect(val).toBe("abc");
    unsub();
  });
});
