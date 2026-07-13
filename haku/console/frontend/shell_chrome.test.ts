import { describe, expect, it } from "vitest";

import { nextShellPanel } from "./shell_chrome.tsx";

describe("nextShellPanel", () => {
  it("opens a closed panel", () => {
    expect(nextShellPanel(null, "settings")).toBe("settings");
  });

  it("closes the selected panel on its second click", () => {
    expect(nextShellPanel("settings", "settings")).toBeNull();
  });

  it("switches directly to another panel", () => {
    expect(nextShellPanel("approvals", "screenshot")).toBe("screenshot");
  });
});
