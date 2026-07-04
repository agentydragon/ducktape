// @vitest-environment jsdom
import { MantineProvider } from "@mantine/core";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { gitProgress } from "./git_progress.ts";
import { GitProgressBar } from "./git_progress_bar.tsx";

window.matchMedia ??= ((query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addListener: () => {},
  removeListener: () => {},
  addEventListener: () => {},
  removeEventListener: () => {},
  dispatchEvent: () => false,
})) as typeof window.matchMedia;

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("GitProgressBar", () => {
  it("shows the bar + ops summary while reads are in flight, then hides once the burst settles", async () => {
    render(
      <MantineProvider>
        <GitProgressBar />
      </MantineProvider>
    );
    // Idle → renders nothing.
    expect(screen.queryByRole("progressbar")).toBeNull();

    const a = deferred<void>();
    const b = deferred<void>();
    let opA!: Promise<void>;
    let opB!: Promise<void>;
    act(() => {
      opA = gitProgress.track(1, () => a.promise); // tree
      opB = gitProgress.track(8, () => b.promise); // a blob chunk
    });
    // In flight → the bar appears with the ops + objects summary.
    expect(screen.getByRole("progressbar")).toBeTruthy();
    expect(screen.getByText(/2 in progress · 0 done · 0\/9 objects/)).toBeTruthy();

    await act(async () => {
      a.resolve();
      await opA;
    });
    // One op done, one still going.
    expect(screen.getByText(/1 in progress · 1 done · 1\/9 objects/)).toBeTruthy();

    await act(async () => {
      b.resolve();
      await opB;
    });
    // Drained but settling → shown at completion.
    expect(screen.getByText(/0 in progress · 2 done · 9\/9 objects/)).toBeTruthy();

    // Settle window elapses → hidden again.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(screen.queryByRole("progressbar")).toBeNull();
  });
});
