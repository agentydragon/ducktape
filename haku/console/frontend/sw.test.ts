// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";

import { notificationActions } from "./sw";

describe("notificationActions", () => {
  it.each([
    [undefined, ["approve", "deny", "details"]],
    [0, []],
    [1, ["approve"]],
    [2, ["approve", "deny"]],
    [3, ["approve", "deny", "details"]],
    [99, ["approve", "deny", "details"]],
  ] as const)("keeps the highest-priority actions that fit (%s)", (maxActions, expected) => {
    expect(notificationActions(maxActions).map(({ action }) => action)).toEqual(expected);
  });
});
