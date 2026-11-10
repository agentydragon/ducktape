import { expect, it } from "vitest";

import { ago, every, lastEvent, nextPoll, plural, sentence, untilExpiry } from "./status";

const NOW = Date.parse("2026-01-01T12:00:00Z");

it.each([
  ["2026-01-01T19:59:00Z", "in 7h 59m"],
  ["2026-01-01T11:59:00Z", "due for refresh"],
  // Sixty rounded minutes roll into the hour.
  ["2026-01-01T12:59:40Z", "in 1h 0m"],
])("untilExpiry(%s) is %s", (instant, expected) => {
  expect(untilExpiry(instant, NOW)).toBe(expected);
});

it.each([
  ["2026-01-01T11:59:30Z", "just now"],
  ["2026-01-01T11:45:00Z", "15m ago"],
])("ago(%s) is %s", (instant, expected) => {
  expect(ago(instant, NOW)).toBe(expected);
});

it("plural keeps the noun singular for exactly one", () => {
  expect(plural(1, "session")).toBe("1 session");
  expect(plural(0, "session")).toBe("0 sessions");
  expect(plural(308, "event")).toBe("308 events");
});

it("every names the largest whole unit", () => {
  expect(every(45)).toBe("45 seconds");
  expect(every(300)).toBe("5 minutes");
  expect(every(60)).toBe("1 minute");
  expect(every(7200)).toBe("2 hours");
});

it.each([
  ["2026-01-01T11:57:00Z", "in 2m"],
  ["2026-01-01T11:50:00Z", "due now"],
])("nextPoll(%s) after a 300-second pause is %s", (instant, expected) => {
  expect(nextPoll(instant, 300, NOW)).toBe(expected);
});

it.each([
  [null, "none yet"],
  ["2026-01-01T11:45:00Z", "15m ago"],
])("lastEvent(%s) is %s", (instant, expected) => {
  expect(lastEvent(instant, NOW)).toBe(expected);
});

it("sentence capitalizes a log-style message and leaves the rest alone", () => {
  expect(sentence("the pasted URL is not from this attempt")).toBe("The pasted URL is not from this attempt");
  expect(sentence("")).toBe("");
});
