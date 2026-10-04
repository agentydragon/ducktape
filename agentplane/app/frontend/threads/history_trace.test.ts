// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LayoutSettle } from "./history_trace";

describe("LayoutSettle", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame"] });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const frames = (count: number) => {
    for (let frame = 0; frame < count; frame++) vi.advanceTimersToNextFrame();
  };

  it("settles once the layout has stayed put for a few frames, and unsettles when it moves", () => {
    const changes: boolean[] = [];
    const settle = new LayoutSettle(
      () => false,
      (settled) => changes.push(settled)
    );
    frames(1);
    expect(changes).toEqual([]);
    frames(20);
    expect(changes).toEqual([true]);

    settle.changed();
    expect(changes).toEqual([true, false]);
    frames(20);
    expect(changes).toEqual([true, false, true]);
  });

  it("is kept unsettled by every change that lands before it has been still for long enough", () => {
    const changes: boolean[] = [];
    const settle = new LayoutSettle(
      () => false,
      (settled) => changes.push(settled)
    );
    for (let change = 0; change < 50; change++) {
      frames(1);
      settle.changed();
    }
    expect(changes).toEqual([]);
  });

  it("does not settle while a correction is still in flight", () => {
    let busy = true;
    const changes: boolean[] = [];
    new LayoutSettle(
      () => busy,
      (settled) => changes.push(settled)
    );
    frames(100);
    expect(changes).toEqual([]);
    busy = false;
    frames(20);
    expect(changes).toEqual([true]);
  });
});
