import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getGitProgress, trackGit } from "./git_progress.ts";

// A resolvable/rejectable promise so a test can hold an op "in flight", inspect the store, then
// drain it deterministically.
function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const IDLE = { activeOps: 0, doneOps: 0, totalObjects: 0, doneObjects: 0 };

// Fake timers so the burst's settle window is advanced explicitly rather than waited on.
beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("git progress tracker", () => {
  it("counts one op in flight, holds through the settle window, then goes idle", async () => {
    const d = deferred<void>();
    const op = trackGit(3, () => d.promise);
    // While in flight: one op active, three objects requested, nothing done yet.
    expect(getGitProgress()).toEqual({ activeOps: 1, doneOps: 0, totalObjects: 3, doneObjects: 0 });
    d.resolve();
    await op;
    // Drained but still within the settle window: op + objects counted done, 100%.
    expect(getGitProgress()).toEqual({ activeOps: 0, doneOps: 1, totalObjects: 3, doneObjects: 3 });
    await vi.advanceTimersByTimeAsync(200);
    expect(getGitProgress()).toEqual(IDLE);
  });

  it("accumulates concurrent ops and advances done-counts as each resolves", async () => {
    const a = deferred<void>();
    const b = deferred<void>();
    const opA = trackGit(1, () => a.promise);
    const opB = trackGit(8, () => b.promise);
    // Two ops active, 9 objects requested across them.
    expect(getGitProgress()).toEqual({ activeOps: 2, doneOps: 0, totalObjects: 9, doneObjects: 0 });
    a.resolve();
    await opA;
    // One op left; the finished op + its object count as done, the burst totals unchanged.
    expect(getGitProgress()).toEqual({ activeOps: 1, doneOps: 1, totalObjects: 9, doneObjects: 1 });
    b.resolve();
    await opB;
    await vi.advanceTimersByTimeAsync(200);
    expect(getGitProgress()).toEqual(IDLE);
  });

  it("coalesces back-to-back reads into one burst (the settle is cancelled by new activity)", async () => {
    const a = deferred<void>();
    const opA = trackGit(1, () => a.promise); // e.g. the tree read
    a.resolve();
    await opA;
    // Settle is now pending at 1 op / 1 object. A second read starting before it fires extends it.
    const b = deferred<void>();
    const opB = trackGit(50, () => b.promise); // e.g. the first blob chunk
    expect(getGitProgress()).toEqual({ activeOps: 1, doneOps: 1, totalObjects: 51, doneObjects: 1 });
    b.resolve();
    await opB;
    expect(getGitProgress()).toEqual({ activeOps: 0, doneOps: 2, totalObjects: 51, doneObjects: 51 });
    await vi.advanceTimersByTimeAsync(200);
    expect(getGitProgress()).toEqual(IDLE);
  });

  it("drains the indicator even when the op throws", async () => {
    const d = deferred<void>();
    const op = trackGit(2, () => d.promise);
    expect(getGitProgress().activeOps).toBe(1);
    d.reject(new Error("boom"));
    await expect(op).rejects.toThrow("boom");
    expect(getGitProgress()).toEqual({ activeOps: 0, doneOps: 1, totalObjects: 2, doneObjects: 2 });
    await vi.advanceTimersByTimeAsync(200);
    expect(getGitProgress()).toEqual(IDLE);
  });
});
