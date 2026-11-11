import { describe, expect, it } from "vitest";

import { money, time } from "./spend_display";

describe("summary money", () => {
  it.each([
    [0, "USD", "$0"],
    [1, "USD", "$0.01"],
    [-1, "USD", "-$0.01"],
    [999, "USD", "$9.99"],
    [1000, "USD", "$10"],
    [1250, "USD", "$13"],
    [9, "JPY", "¥9"],
    [1, "KWD", "KWD 0.001"],
  ])("formats %s minor units of %s", (amount, currency, expected) => {
    expect(money(amount, currency)).toBe(expected);
  });

  it("keeps exact amounts available in details", () => {
    expect(money(1250, "USD", true)).toBe("$12.50");
  });
});

describe("summary timestamps", () => {
  it("uses local time and shows a year or clock only when needed", () => {
    const now = new Date(2026, 11, 20, 12, 0);
    const nearby = new Date(now.getTime() + 14 * 86400_000);
    const distant = new Date(nearby.getTime() + 1000);
    expect(time(nearby.toISOString(), now)).toBe("Jan 3, 2027, 12:00");
    expect(time(distant.toISOString(), now)).toBe("Jan 3, 2027");
    expect(time(new Date(2026, 11, 19, 12).toISOString(), now)).toBe("Dec 19, 12:00");
    expect(time("not a date", now)).toBe("Unknown");
  });
});
