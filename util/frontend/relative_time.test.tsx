// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RelativeTime } from "./relative_time";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
let mounted: boolean;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-22T12:00:00Z"));
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  mounted = true;
});

afterEach(async () => {
  if (mounted) await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
});

describe("RelativeTime", () => {
  it("renders caller formatting and refreshes all timestamps from one clock", async () => {
    const format = (_value: Date) => ({ text: new Date().toISOString() });
    const first = new Date(0);
    const second = new Date("2026-10-22T11:58:00Z");
    await act(async () =>
      root.render(
        <>
          <RelativeTime value={first} format={format} />
          <RelativeTime value={second} format={format} />
        </>
      )
    );

    expect([...container.querySelectorAll("time")].map((element) => element.textContent)).toEqual([
      "2026-10-22T12:00:00.000Z",
      "2026-10-22T12:00:00.000Z",
    ]);
    expect(container.querySelector("time")?.getAttribute("dateTime")).toBe("1970-01-01T00:00:00.000Z");
    expect(vi.getTimerCount()).toBe(1);

    await act(async () => vi.advanceTimersByTime(30_000));

    expect([...container.querySelectorAll("time")].map((element) => element.textContent)).toEqual([
      "2026-10-22T12:00:30.000Z",
      "2026-10-22T12:00:30.000Z",
    ]);
    await act(async () => root.unmount());
    mounted = false;
    expect(vi.getTimerCount()).toBe(0);
  });

  it("keeps an app-provided hover title", async () => {
    await act(async () =>
      root.render(
        <RelativeTime
          value={new Date("2026-10-22T11:59:00Z")}
          format={() => ({ text: "1 minute ago", title: "October 22, 2026 at 11:59 AM" })}
        />
      )
    );

    expect(container.querySelector("time")?.title).toBe("October 22, 2026 at 11:59 AM");
  });
});
