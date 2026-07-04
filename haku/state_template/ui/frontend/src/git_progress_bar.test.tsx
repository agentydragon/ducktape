// @vitest-environment jsdom
import { MantineProvider } from "@mantine/core";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { trackGit } from "./git_progress.ts";
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
  it("shows a bar while a git read is in flight, then hides once the burst settles", async () => {
    render(
      <MantineProvider>
        <GitProgressBar />
      </MantineProvider>
    );
    // Idle → renders nothing.
    expect(screen.queryByRole("progressbar")).toBeNull();

    const d = deferred<void>();
    let op!: Promise<void>;
    act(() => {
      op = trackGit(2, () => d.promise);
    });
    // In flight → the bar appears, labelled with the object counts.
    expect(screen.getByRole("progressbar")).toBeTruthy();
    expect(screen.getByLabelText(/Fetching repository content: 0\/2 objects/)).toBeTruthy();

    await act(async () => {
      d.resolve();
      await op;
    });
    // Drained but settling → still shown at 100%.
    expect(screen.getByLabelText(/Fetching repository content: 2\/2 objects/)).toBeTruthy();

    // Settle window elapses → hidden again.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(screen.queryByRole("progressbar")).toBeNull();
  });
});
