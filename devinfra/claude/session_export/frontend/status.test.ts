import { describe, expect, it } from "vitest";

import { ago, liveSummary, sentence, untilExpiry } from "./status";

const NOW = Date.parse("2026-01-01T12:00:00Z");

describe("untilExpiry", () => {
  it("counts hours and minutes ahead", () => {
    expect(untilExpiry("2026-01-01T19:59:00Z", NOW)).toBe("in 7h 59m");
  });

  it("says a refresh is due once the expiry has passed", () => {
    expect(untilExpiry("2026-01-01T11:59:00Z", NOW)).toBe("due for refresh");
  });

  it("rolls sixty rounded minutes into the hour instead of printing 60m", () => {
    expect(untilExpiry("2026-01-01T12:59:40Z", NOW)).toBe("in 1h 0m");
  });
});

describe("ago", () => {
  it("is 'just now' inside the first minute", () => {
    expect(ago("2026-01-01T11:59:30Z", NOW)).toBe("just now");
  });

  it("counts minutes back", () => {
    expect(ago("2026-01-01T11:45:00Z", NOW)).toBe("15m ago");
  });
});

describe("liveSummary", () => {
  const following = { following: true, watching: true, streams: 2, last_frame_at: "2026-01-01T11:45:00Z" };

  it("says polling is all there is when following is off", () => {
    expect(liveSummary({ ...following, following: false, watching: false, streams: 0 }, NOW)).toBe(
      "Off; the sync polls only"
    );
  });

  it("reports the watch, the open streams and the last frame", () => {
    expect(liveSummary(following, NOW)).toBe("watching for new sessions, 2 sessions streaming, last frame 15m ago");
  });

  it("says so when no frame has arrived and the watch is down", () => {
    expect(liveSummary({ ...following, watching: false, streams: 1, last_frame_at: null }, NOW)).toBe(
      "not watching, 1 session streaming, no frame yet"
    );
  });
});

describe("sentence", () => {
  it("capitalizes a log-style message and leaves the rest alone", () => {
    expect(sentence("the pasted URL is not from this attempt")).toBe("The pasted URL is not from this attempt");
    expect(sentence("")).toBe("");
  });
});
