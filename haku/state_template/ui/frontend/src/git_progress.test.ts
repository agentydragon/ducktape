import { describe, expect, it } from "vitest";

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

describe("git progress tracker", () => {
  it("counts a single op in flight, then drains to idle on completion", async () => {
    const d = deferred<void>();
    const op = trackGit(3, () => d.promise);
    // While in flight: one op, three objects requested, none done yet.
    expect(getGitProgress()).toEqual({ inFlight: 1, total: 3, done: 0 });
    d.resolve();
    await op;
    // Burst emptied → back to idle.
    expect(getGitProgress()).toEqual({ inFlight: 0, total: 0, done: 0 });
  });

  it("accumulates concurrent ops and advances `done` as each resolves", async () => {
    const a = deferred<void>();
    const b = deferred<void>();
    const opA = trackGit(1, () => a.promise);
    const opB = trackGit(8, () => b.promise);
    // Both in flight: 9 objects requested across two ops.
    expect(getGitProgress()).toEqual({ inFlight: 2, total: 9, done: 0 });
    a.resolve();
    await opA;
    // One op left; the finished op's object counts toward `done`, the burst total unchanged.
    expect(getGitProgress()).toEqual({ inFlight: 1, total: 9, done: 1 });
    b.resolve();
    await opB;
    expect(getGitProgress()).toEqual({ inFlight: 0, total: 0, done: 0 });
  });

  it("drains the indicator even when the op throws", async () => {
    const d = deferred<void>();
    const op = trackGit(2, () => d.promise);
    expect(getGitProgress().inFlight).toBe(1);
    d.reject(new Error("boom"));
    await expect(op).rejects.toThrow("boom");
    expect(getGitProgress()).toEqual({ inFlight: 0, total: 0, done: 0 });
  });
});
