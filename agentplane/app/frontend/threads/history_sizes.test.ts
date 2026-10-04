import { describe, expect, it } from "vitest";

import { rememberRowHeight, rememberedRowHeight } from "./history_sizes";

describe("row heights", () => {
  it("are only used for the thread and the width they were read at", () => {
    rememberRowHeight("thread-a", 412, "item:1", 96);
    expect(rememberedRowHeight("thread-a", 412, "item:1")).toBe(96);
    expect(rememberedRowHeight("thread-a", 413, "item:1")).toBeUndefined();
    expect(rememberedRowHeight("thread-b", 412, "item:1")).toBeUndefined();
    expect(rememberedRowHeight("thread-a", 412, "item:2")).toBeUndefined();
  });

  it("are looked up at the last width read when the history has no width yet", () => {
    rememberRowHeight("thread-c", 1008, "item:1", 40);
    expect(rememberedRowHeight("thread-c", 0, "item:1")).toBe(40);
  });

  it("ignore a reading from an element that is not laid out", () => {
    rememberRowHeight("thread-d", 0, "item:1", 40);
    rememberRowHeight("thread-d", 412, "item:2", 0);
    expect(rememberedRowHeight("thread-d", 412, "item:1")).toBeUndefined();
    expect(rememberedRowHeight("thread-d", 412, "item:2")).toBeUndefined();
  });

  it("forget the oldest reading rather than grow without bound", () => {
    rememberRowHeight("thread-e", 412, "oldest", 50);
    for (let row = 0; row < 20_000; row++) rememberRowHeight("thread-e", 412, `row:${row}`, 50);
    expect(rememberedRowHeight("thread-e", 412, "oldest")).toBeUndefined();
    expect(rememberedRowHeight("thread-e", 412, "row:19999")).toBe(50);
  });
});
