import { describe, expect, it } from "vitest";

import { formatDurationShort, formatTimestamp, formatWindowDuration, secondsUntil, shortDate } from "./time";

describe("time formatting", () => {
  const now = Date.parse("2026-08-29T12:00:00Z");

  it("uses a relative label for a nearby instant and retains the full title", () => {
    const timestamp = formatTimestamp("2026-08-29T10:00:00Z", now);

    expect(timestamp.text).toBe("2 hours ago");
    expect(timestamp.title).not.toBe("");
    expect(timestamp.isFresh).toBe(false);
  });

  it("marks a fresh observation without changing the library's relative label", () => {
    const timestamp = formatTimestamp("2026-08-29T11:59:59Z", now);

    expect(timestamp.text).toBe("1 second ago");
    expect(timestamp.isFresh).toBe(true);
  });

  it("returns null for an absent short date", () => {
    expect(shortDate(null)).toBeNull();
    expect(shortDate("2026-08-29T12:00:00Z")).not.toBeNull();
  });

  it("formats durations through date-fns", () => {
    expect(formatDurationShort(90 * 60)).toBe("1 hour 30 minutes");
    expect(formatWindowDuration(24 * 60 * 60)).toBe("1 day");
  });

  it("calculates seconds until a timestamp through date-fns", () => {
    expect(secondsUntil("2026-08-29T12:01:30Z", now)).toBe(90);
    expect(secondsUntil("2026-08-29T11:59:30Z", now)).toBe(0);
  });
});
