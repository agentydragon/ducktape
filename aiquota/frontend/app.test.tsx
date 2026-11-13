// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./app";

class Stream extends EventTarget {
  static CLOSED = 2;
  static instances: Stream[] = [];
  readyState = 1;
  onerror: (() => void) | null = null;
  close = vi.fn();
  constructor(readonly url: string) {
    super();
    Stream.instances.push(this);
  }
  view(fetchedAt: string): void {
    this.dispatchEvent(
      new MessageEvent("view", {
        data: JSON.stringify({ fetched_at: fetchedAt, providers: [] }),
      })
    );
  }
}

let root: Root;
let container: HTMLDivElement;

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("EventSource", Stream);
  vi.stubGlobal("fetch", vi.fn());
  Stream.instances = [];
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  act(() =>
    root.render(
      <MantineProvider env="test">
        <App />
      </MantineProvider>
    )
  );
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("quota subscription", () => {
  it("subscribes to server views without fetching or polling", () => {
    const stream = Stream.instances[0]!;
    expect(stream.url).toBe("/v1/events");
    expect(container.querySelector('[role="status"]')?.textContent).toBe("Connecting to live updates…");
    expect(container.querySelector<HTMLButtonElement>("button")?.textContent).toContain("Reconnect");
    expect(container.querySelector<HTMLButtonElement>("button")?.disabled).toBe(true);

    act(() => stream.view("2026-01-15T12:00:00Z"));
    expect(container.querySelector('[role="status"]')?.textContent).toBe("Live updates connected");
    expect(container.textContent).toContain("No quota providers are available.");
    expect(container.querySelector(".aiquota-connection-help")).toBeNull();
    act(() => {
      vi.advanceTimersByTime(600_000);
    });
    expect(fetch).not.toHaveBeenCalled();
    expect(Stream.instances).toHaveLength(1);
  });

  it("retains the snapshot and explains the automatic retry after an interrupted stream", () => {
    const stream = Stream.instances[0]!;
    act(() => stream.view("2026-01-15T12:00:00Z"));
    const initialBoard = container.querySelector(".aiquota-board")?.textContent;

    act(() => stream.onerror?.());
    expect(container.querySelector('[role="status"]')?.textContent).toBe("Live updates unavailable");
    expect(container.querySelector(".aiquota-notice")?.textContent).toContain("browser is retrying");
    expect(container.querySelector(".aiquota-connection-help")?.textContent).toContain("use Reconnect");
    expect(container.querySelector(".aiquota-board")?.textContent).toBe(initialBoard);
    expect(container.querySelector<HTMLButtonElement>("button")?.disabled).toBe(false);

    act(() => container.querySelector<HTMLButtonElement>("button")?.click());
    expect(stream.close).toHaveBeenCalledOnce();
    expect(Stream.instances).toHaveLength(2);
  });

  it("keeps the closed-stream reload recovery path distinct from an interrupted retry", () => {
    const stream = Stream.instances[0]!;
    stream.readyState = Stream.CLOSED;
    act(() => stream.onerror?.());

    expect(container.querySelector('[role="status"]')?.textContent).toBe("Live updates unavailable");
    expect(container.querySelector(".aiquota-notice")?.textContent).toContain("Reload to start a fresh stream");
    expect(container.querySelector(".aiquota-connection-help")?.textContent).toContain("will reload the page");
    expect(container.querySelector<HTMLButtonElement>("button")?.disabled).toBe(false);
  });
});
