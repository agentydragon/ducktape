import { describe, expect, it } from "vitest";

import { ago, every, lastEvent, nextPoll, plural, sentence, untilExpiry } from "./status";

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

describe("plural", () => {
  it("keeps the noun singular for exactly one", () => {
    expect(plural(1, "session")).toBe("1 session");
    expect(plural(0, "session")).toBe("0 sessions");
    expect(plural(308, "event")).toBe("308 events");
  });
});

describe("every", () => {
  it("names the largest whole unit", () => {
    expect(every(45)).toBe("45 seconds");
    expect(every(300)).toBe("5 minutes");
    expect(every(60)).toBe("1 minute");
    expect(every(7200)).toBe("2 hours");
  });
});

describe("nextPoll", () => {
  it("counts down from when the last poll finished", () => {
    expect(nextPoll("2026-01-01T11:57:00Z", 300, NOW)).toBe("in 2m");
  });

  it("says a poll is due once the pause has passed", () => {
    expect(nextPoll("2026-01-01T11:50:00Z", 300, NOW)).toBe("due now");
  });
});

describe("lastEvent", () => {
  it("says none yet before any event arrived", () => {
    expect(lastEvent(null, NOW)).toBe("none yet");
  });

  it("counts back from the last event", () => {
    expect(lastEvent("2026-01-01T11:45:00Z", NOW)).toBe("15m ago");
  });
});

describe("sentence", () => {
  it("capitalizes a log-style message and leaves the rest alone", () => {
    expect(sentence("the pasted URL is not from this attempt")).toBe("The pasted URL is not from this attempt");
    expect(sentence("")).toBe("");
  });
});
