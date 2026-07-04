// @vitest-environment jsdom
import { MantineProvider } from "@mantine/core";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

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

afterEach(cleanup);

describe("GitProgressBar", () => {
  it("shows a bar only while a git read is in flight, then hides when the burst drains", async () => {
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
    // Drained → hidden again.
    expect(screen.queryByRole("progressbar")).toBeNull();
  });
});
