// @vitest-environment happy-dom
import { act, screen } from "@testing-library/react";
import type { JSX } from "react";
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
import { renderInMantine } from "./testing_library";

const KEY = Symbol("test stream");

beforeEach(() => vi.useFakeTimers({ now: new Date(2026, 0, 1, 17, 21, 4) }));

afterEach(() => {
  streamRegistry.remove(KEY);
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

function Page({ connection }: { connection: StreamConnection }): JSX.Element {
  const stream = useStreamStatus("Actions", connection);
  return <StaleNotice streams={[stream]} />;
}

it("shows nothing within the grace, then a spinner naming the stream, then the page's own notice", async () => {
  renderInMantine(<ConnectionIndicator />);
  const page = renderInMantine(
    <Page connection={{ phase: "reconnecting", since: Date.now(), attempt: 3, lastError: "HTTP 503" }} />
  );
  expect(screen.queryByRole("img")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();

  await act(async () => vi.advanceTimersByTime(DEGRADED_AFTER_MS - 1));
  expect(screen.queryByRole("img")).toBeNull();
  await act(async () => vi.advanceTimersByTime(1));
  const spinner = screen.getByRole("img", { name: "Actions: reconnecting since 17:21:04 · attempt 3 · HTTP 503" });
  expect(spinner).toHaveAttribute("data-connection", "degraded");
  expect(screen.queryByRole("alert")).toBeNull();

  await act(async () => vi.advanceTimersByTime(STALE_AFTER_MS - DEGRADED_AFTER_MS));
  expect(screen.getByRole("img")).toHaveAttribute("data-connection", "stale");
  expect(screen.getByRole("alert")).toHaveTextContent(/^What's on screen may be out of date; last update 17:21:04$/);

  // The stream goes with the page that followed it.
  page.unmount();
  expect(screen.queryByRole("img")).toBeNull();
});
