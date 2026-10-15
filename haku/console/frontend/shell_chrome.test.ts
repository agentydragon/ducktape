import { describe, expect, it } from "vitest";

import { syncState } from "./shell_chrome";

describe("syncState", () => {
  it("reports an error before any lower-priority state", () => {
    expect(syncState("Unauthorized", true, new Date())).toBe("error");
  });

  it("reports a first or active refresh as syncing", () => {
    expect(syncState(null, false, null)).toBe("syncing");
    expect(syncState(null, true, new Date())).toBe("syncing");
  });

  it("reports current after a successful REST refresh", () => {
    expect(syncState(null, false, new Date())).toBe("current");
  });
});
