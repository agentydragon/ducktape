// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act, type JSX, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { StreamConnection } from "./live_stream";
import {
  ConnectionIndicator,
  DEGRADED_AFTER_MS,
  STALE_AFTER_MS,
  StaleNotice,
  streamRegistry,
  useStreamStatus,
} from "./stream_status";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const KEY = Symbol("test stream");
const roots: ReturnType<typeof createRoot>[] = [];

beforeEach(() => vi.useFakeTimers({ now: new Date(2026, 0, 1, 17, 21, 4) }));

afterEach(async () => {
  for (const root of roots.splice(0)) await act(async () => root.unmount());
  streamRegistry.remove(KEY);
  document.body.replaceChildren();
  vi.useRealTimers();
});

function reconnecting(since: number, attempt = 1): StreamConnection {
  return { phase: "reconnecting", since, attempt, lastError: null };
}

function standing(): string | undefined {
  return streamRegistry.getStatuses().get(KEY)?.standing;
}

it("counts a stream degraded once it has been off for the grace without a break, and stale at a minute", () => {
  const since = Date.now();
  streamRegistry.report(KEY, "Test", reconnecting(since));
  vi.advanceTimersByTime(DEGRADED_AFTER_MS - 2_000);
  // A later failure of the same outage does not restart its clock.
  streamRegistry.report(KEY, "Test", reconnecting(since, 2));
  vi.advanceTimersByTime(1_999);
  expect(standing()).toBe("current");
  vi.advanceTimersByTime(1);
  expect(standing()).toBe("degraded");
  vi.advanceTimersByTime(STALE_AFTER_MS - DEGRADED_AFTER_MS - 1);
  expect(standing()).toBe("degraded");
  vi.advanceTimersByTime(1);
  expect(standing()).toBe("stale");

  streamRegistry.report(KEY, "Test", { phase: "live", since: Date.now() });
  expect(standing()).toBe("current");
});

it("never counts a stream that recovers within the grace, and gives its next drop the whole grace again", () => {
  streamRegistry.report(KEY, "Test", reconnecting(Date.now()));
  vi.advanceTimersByTime(DEGRADED_AFTER_MS - 1);
  streamRegistry.report(KEY, "Test", { phase: "live", since: Date.now() });
  // Nothing is left to wake for.
  expect(vi.getTimerCount()).toBe(0);

  streamRegistry.report(KEY, "Test", reconnecting(Date.now()));
  vi.advanceTimersByTime(DEGRADED_AFTER_MS - 1);
  expect(standing()).toBe("current");
  vi.advanceTimersByTime(1);
  expect(standing()).toBe("degraded");
});

function mount(element: ReactNode): HTMLDivElement {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  roots.push(root);
  act(() => root.render(<MantineProvider env="test">{element}</MantineProvider>));
  return container;
}

function Page({ connection }: { connection: StreamConnection }): JSX.Element {
  const stream = useStreamStatus("Actions", connection);
  return <StaleNotice streams={[stream]} />;
}

it("shows nothing within the grace, then a spinner naming the stream, then the page's own notice", async () => {
  const indicator = mount(<ConnectionIndicator />);
  const page = mount(
    <Page connection={{ phase: "reconnecting", since: Date.now(), attempt: 3, lastError: "HTTP 503" }} />
  );
  expect(indicator.querySelector("[data-connection]")).toBeNull();
  expect(page.querySelector('[role="alert"]')).toBeNull();

  await act(async () => vi.advanceTimersByTime(DEGRADED_AFTER_MS - 1));
  expect(indicator.querySelector("[data-connection]")).toBeNull();
  await act(async () => vi.advanceTimersByTime(1));
  const spinner = indicator.querySelector("[data-connection]");
  expect(spinner?.getAttribute("data-connection")).toBe("degraded");
  expect(spinner?.getAttribute("aria-label")).toBe("Actions: reconnecting since 17:21:04 · attempt 3 · HTTP 503");
  expect(page.querySelector('[role="alert"]')).toBeNull();

  await act(async () => vi.advanceTimersByTime(STALE_AFTER_MS - DEGRADED_AFTER_MS));
  expect(indicator.querySelector("[data-connection]")?.getAttribute("data-connection")).toBe("stale");
  expect(page.querySelector('[role="alert"]')?.textContent).toBe(
    "What's on screen may be out of date; last update 17:21:04"
  );

  // The stream goes with the page that followed it.
  const [pageRoot] = roots.splice(1, 1);
  await act(async () => pageRoot.unmount());
  expect(indicator.querySelector("[data-connection]")).toBeNull();
});
