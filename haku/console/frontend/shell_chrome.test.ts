import { describe, expect, it } from "vitest";

import { nextShellPanel, selectedShellPanel } from "./shell_chrome.tsx";

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

describe("selectedShellPanel", () => {
  it("auto-opens approvals when no panel is selected", () => {
    expect(selectedShellPanel(true, null)).toBe("approvals");
  });

  it("does not let a new approval preempt an operator-selected safety panel", () => {
    expect(selectedShellPanel(true, "location")).toBe("location");
    expect(selectedShellPanel(true, "screenshot")).toBe("screenshot");
  });
});
