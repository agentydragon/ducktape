// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./app";
import type { QuotasView } from "./quotas";

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
  view(fetchedAt: string, resets = 1): void {
    this.dispatchEvent(
      new MessageEvent("view", {
        data: JSON.stringify({
          fetched_at: fetchedAt,
          providers: [
            {
              provider: "codex",
              last_output: {
                fetched_at: fetchedAt,
                result: {
                  kind: "success",
                  windows: [],
                  available_reset_credits: resets,
                  reset_credit_expiry_lookup_failed: false,
                },
              },
              last_success: null,
              currently_over_plan: false,
              paid_credits_active: false,
              extra_status: "none",
              burn: null,
            },
          ],
        } satisfies QuotasView),
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
  act(() => root.render(<App />));
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("quota subscription", () => {
  it.each(["{", "{}", '{"fetched_at":"2026-01-15T12:00:00Z","providers":[{"last_output":false}]}'])(
    "retains the last valid snapshot and recovers after a rejected frame: %s",
    (data) => {
      const warning = vi.spyOn(console, "warn").mockImplementation(() => undefined);
      const stream = Stream.instances[0];
      act(() => stream.view("2026-01-15T12:00:00+00:00"));
      expect(container.textContent).toContain("1 banked reset");
      const initial = container.querySelector(".aiquota-card")?.textContent;

      act(() => {
        stream.dispatchEvent(new MessageEvent("view", { data }));
      });
      expect(container.querySelector('[role="status"]')?.textContent).toBe("Quota stream unavailable");
      expect(container.querySelector(".aiquota-card")?.textContent).toBe(initial);
      expect(warning).toHaveBeenCalledOnce();

      act(() => stream.view("2026-01-16T12:00:00Z", 2));
      expect(container.querySelector(".aiquota-notice")).toBeNull();
      expect(container.querySelector('[role="status"]')?.textContent).toBe("Quota stream live");
      expect(container.textContent).toContain("2 banked resets");
    }
  );

  it("reports an invalid first frame without leaving reconnect disabled", () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    act(() => {
      Stream.instances[0].dispatchEvent(new MessageEvent("view", { data: "{}" }));
    });
    expect(container.querySelector('[role="status"]')?.textContent).toBe("Quota stream unavailable");
    expect(container.querySelector<HTMLButtonElement>("button")?.disabled).toBe(false);
    expect(container.querySelector(".aiquota-card")).toBeNull();
  });

  it("receives initial and replacement snapshots without fetching or polling", () => {
    const stream = Stream.instances[0];
    expect(stream.url).toBe("/v1/events");
    expect(container.querySelector('[role="status"]')?.textContent).toContain("Connecting");
    act(() => stream.view("2026-01-15T12:00:00Z"));
    expect(container.querySelector('[role="status"]')?.textContent).toBe("Quota stream live");
    expect(container.textContent).toContain("1 banked reset");
    act(() => stream.view("2026-01-16T12:00:00Z", 2));
    expect(container.textContent).toContain("2 banked resets");
    expect(container.textContent).not.toContain("checked");
    expect(container.textContent).not.toContain("Snapshot");
    act(() => {
      vi.advanceTimersByTime(600_000);
    });
    expect(fetch).not.toHaveBeenCalled();
    expect(Stream.instances).toHaveLength(1);
  });

  it("renders Claude's exact prepaid balance as money, separate from Codex credits", () => {
    const stream = Stream.instances[0];
    act(() => {
      stream.dispatchEvent(
        new MessageEvent("view", {
          data: JSON.stringify({
            fetched_at: "2026-01-15T12:00:00Z",
            providers: [
              {
                provider: "claude",
                last_output: {
                  fetched_at: "2026-01-15T12:00:00Z",
                  result: {
                    kind: "success",
                    windows: [],
                    prepaid_balance: { kind: "available", amount_minor: 12345, exponent: 2, currency: "USD" },
                  },
                },
                last_success: null,
                currently_over_plan: false,
                paid_credits_active: false,
                extra_status: "none",
                burn: null,
              },
            ],
          }),
        })
      );
    });

    expect(container.textContent).toContain("Claude prepaid balance · $123.45");
    expect(container.textContent).not.toContain("No quota data.");
    expect(container.textContent).not.toContain("Paid credits");

    act(() => {
      stream.dispatchEvent(
        new MessageEvent("view", {
          data: JSON.stringify({
            fetched_at: "2026-01-15T12:00:00Z",
            providers: [
              {
                provider: "claude",
                last_output: {
                  fetched_at: "2026-01-15T12:00:00Z",
                  result: { kind: "success", windows: [], prepaid_balance: { kind: "unavailable" } },
                },
                last_success: null,
                currently_over_plan: false,
                paid_credits_active: false,
                extra_status: "none",
                burn: null,
              },
            ],
          }),
        })
      );
    });
    expect(container.textContent).toContain("Claude prepaid balance unavailable");
  });

  it("retains the snapshot through a disconnect and clears the error on recovery", () => {
    const stream = Stream.instances[0];
    act(() => stream.view("2026-01-15T12:00:00Z"));
    const initial = container.querySelector(".aiquota-card")?.textContent;
    act(() => stream.onerror?.());
    expect(container.querySelector(".aiquota-notice")?.textContent).toContain("Reconnecting");
    expect(container.querySelector('[role="status"]')?.textContent).toBe("Quota stream unavailable");
    expect(container.querySelector(".aiquota-card")?.textContent).toBe(initial);
    act(() => stream.view("2026-01-16T12:00:00Z"));
    expect(container.querySelector(".aiquota-notice")).toBeNull();
    expect(container.querySelector('[role="status"]')?.textContent).toBe("Quota stream live");
  });

  it("closes a replaced subscription and ignores its late callbacks", () => {
    const previous = Stream.instances[0];
    act(() => previous.view("2026-01-15T12:00:00Z"));
    act(() => container.querySelector<HTMLButtonElement>("button")?.click());
    expect(previous.close).toHaveBeenCalledOnce();
    const current = Stream.instances[1];
    act(() => current.view("2026-01-16T12:00:00Z", 2));
    const latest = container.querySelector(".aiquota-card")?.textContent;
    act(() => {
      previous.view("2026-01-14T12:00:00Z");
      previous.onerror?.();
    });
    expect(container.querySelector(".aiquota-card")?.textContent).toBe(latest);
    expect(container.querySelector(".aiquota-notice")).toBeNull();
    act(() => root.unmount());
    expect(current.close).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });
});
