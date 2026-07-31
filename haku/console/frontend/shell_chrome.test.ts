import { describe, expect, it } from "vitest";

import { syncState } from "./shell_chrome";

describe("syncState", () => {
  it("reports an error before any lower-priority state", () => {
    expect(syncState("offline", null, true)).toBe("error");
    expect(syncState("live", "Unauthorized", true)).toBe("error");
  });

  it("reports connecting and active refreshes as syncing", () => {
    expect(syncState("connecting", null, false)).toBe("syncing");
    expect(syncState("live", null, true)).toBe("syncing");
  });

  it("only reports current after connection and refresh both succeed", () => {
    expect(syncState("live", null, false)).toBe("current");
  });
});
