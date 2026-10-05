import { describe, expect, it } from "vitest";

import { THREAD_STATUS_MARKS } from "./status_mark";
import { appDocumentTitle, CONNECTING_TAB_STATUS, threadDocumentTitle } from "./tab_metadata";

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

  it("leads with the status glyph, then the thread name and status label, falling back to a short id", () => {
    expect(threadDocumentTitle("  Review PR  ", "12345678-0000", { kind: "running", tabLabel: "Running" })).toBe(
      `${THREAD_STATUS_MARKS.running.glyph} Review PR · Running — Agentplane`
    );
    expect(threadDocumentTitle(null, "12345678-0000", CONNECTING_TAB_STATUS)).toBe(
      `${THREAD_STATUS_MARKS.inactive.glyph} Thread 12345678 · Connecting — Agentplane`
    );
    expect(threadDocumentTitle("Old spike", "12345678-0000", { kind: "archived", tabLabel: "Archived" })).toBe(
      `${THREAD_STATUS_MARKS.archived.glyph} Old spike · Archived — Agentplane`
    );
  });

  it("gives every thread status its own glyph, and never one a platform may draw as emoji", () => {
    const glyphs = Object.values(THREAD_STATUS_MARKS).map((mark) => mark.glyph);
    expect(new Set(glyphs).size).toBe(glyphs.length);
    for (const glyph of glyphs) expect(glyph).not.toMatch(/\p{Emoji_Presentation}|\uFE0F/u);
  });
});
