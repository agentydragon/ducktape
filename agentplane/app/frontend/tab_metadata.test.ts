import { describe, expect, it } from "vitest";

import { appDocumentTitle, threadDocumentTitle } from "./tab_metadata";

describe("browser tab titles", () => {
  it.each([
    ["/", false, "Agentplane"],
    ["/actions", false, "Actions — Agentplane"],
    ["/actions/request-1", false, "Actions — Agentplane"],
    ["/sandboxes", false, "Sandboxes — Agentplane"],
    ["/sandboxes/work%20space", false, "work space — Agentplane"],
    ["/connection-enrollments/handle", false, "Connect account — Agentplane"],
    ["/actions", true, "Settings — Agentplane"],
  ])("names %s with settings=%s", (path, settingsOpen, expected) => {
    expect(appDocumentTitle(path, settingsOpen)).toBe(expected);
  });

  it("uses the thread name and current status, falling back to a short id", () => {
    expect(threadDocumentTitle("  Review PR  ", "12345678-0000", "Running")).toBe("Review PR · Running — Agentplane");
    expect(threadDocumentTitle(null, "12345678-0000", "Connecting")).toBe("Thread 12345678 · Connecting — Agentplane");
  });
});
