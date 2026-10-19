// @vitest-environment happy-dom
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
    this.dispatchEvent(new MessageEvent("view", { data: JSON.stringify({ fetched_at: fetchedAt, providers: [] }) }));
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
  act(() => root.render(<App />));
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("quota subscription", () => {
  it("receives initial and replacement snapshots without fetching or polling", () => {
    const stream = Stream.instances[0];
    expect(stream.url).toBe("/v1/events");
    act(() => stream.view("2026-01-15T12:00:00Z"));
    const initial = container.querySelector(".aiquota-snapshot")?.textContent;
    expect(initial).toBeTruthy();
    act(() => stream.view("2026-01-16T12:00:00Z"));
    expect(container.querySelector(".aiquota-snapshot")?.textContent).not.toBe(initial);
    act(() => {
      vi.advanceTimersByTime(600_000);
    });
    expect(fetch).not.toHaveBeenCalled();
    expect(Stream.instances).toHaveLength(1);
  });

  it("retains the snapshot through a disconnect and clears the error on recovery", () => {
    const stream = Stream.instances[0];
    act(() => stream.view("2026-01-15T12:00:00Z"));
    const initial = container.querySelector(".aiquota-snapshot")?.textContent;
    act(() => stream.onerror?.());
    expect(container.querySelector(".aiquota-notice")?.textContent).toContain("Reconnecting");
    expect(container.querySelector(".aiquota-snapshot")?.textContent).toBe(initial);
    act(() => stream.view("2026-01-16T12:00:00Z"));
    expect(container.querySelector(".aiquota-notice")).toBeNull();
  });

  it("closes a replaced subscription and ignores its late callbacks", () => {
    const previous = Stream.instances[0];
    act(() => previous.view("2026-01-15T12:00:00Z"));
    act(() => container.querySelector<HTMLButtonElement>("button")?.click());
    expect(previous.close).toHaveBeenCalledOnce();
    const current = Stream.instances[1];
    act(() => current.view("2026-01-16T12:00:00Z"));
    const latest = container.querySelector(".aiquota-snapshot")?.textContent;
    act(() => {
      previous.view("2026-01-14T12:00:00Z");
      previous.onerror?.();
    });
    expect(container.querySelector(".aiquota-snapshot")?.textContent).toBe(latest);
    expect(container.querySelector(".aiquota-notice")).toBeNull();
    act(() => root.unmount());
    expect(current.close).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });
});
