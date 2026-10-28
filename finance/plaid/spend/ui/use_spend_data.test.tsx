// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { SpendTransactionsView, SpendView } from "./schema";
import { useSpendData } from "./use_spend_data";
import type { SpendTab, TransactionPeriodId } from "./types";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

class TestEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;

  static instances: TestEventSource[] = [];

  readyState = TestEventSource.CONNECTING;
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  closed = false;
  private listeners = new Map<string, Set<EventListenerOrEventListenerObject>>();

  constructor(readonly url: string) {
    TestEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    const listeners = this.listeners.get(type) ?? new Set<EventListenerOrEventListenerObject>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  close(): void {
    this.closed = true;
    this.readyState = TestEventSource.CLOSED;
  }

  open(): void {
    this.readyState = TestEventSource.OPEN;
    this.onopen?.(new Event("open"));
  }

  failWhileConnecting(): void {
    this.readyState = TestEventSource.CONNECTING;
    this.onerror?.(new Event("error"));
  }

  sendView(value: unknown): void {
    const event = new MessageEvent("view", { data: typeof value === "string" ? value : JSON.stringify(value) });
    for (const listener of this.listeners.get("view") ?? []) {
      if (typeof listener === "function") listener(event);
      else listener.handleEvent(event);
    }
  }
}

interface ProbeProps {
  activeTab: SpendTab;
  transactionPeriodId: TransactionPeriodId;
}

function Probe({ activeTab, transactionPeriodId }: ProbeProps) {
  const spend = useSpendData({ activeTab, selectedEstimatePeriodId: null, transactionPeriodId });
  return (
    <output>{`${spend.connectionState}|${spend.view ? "view" : "no-view"}|${spend.error ?? "no-error"}|${spend.transactions?.requested_period_id ?? "no-period"}|${spend.transactionsLoading ? "loading" : "loaded"}|${spend.transactionsErrorPeriodId ?? "no-error-period"}|${spend.transactionsError ?? "no-tx-error"}`}</output>
  );
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function transactionView(periodId: TransactionPeriodId): SpendTransactionsView {
  return {
    generated_at: "2026-01-01T00:00:00Z",
    requested_period_id: periodId,
    period: {
      id: periodId,
      start: periodId === "credit_cycle" ? "2025-12-15" : "2025-12-02",
      end: "2025-12-31",
    },
    summary: {
      transaction_count: 0,
      net_allowance_spend_minor_units: 0,
      unmatched_charge_count: 0,
      unmatched_charge_minor_units: 0,
    },
    allowance: null,
    rows: [],
  } satisfies SpendTransactionsView;
}

function transactionResponse(periodId: TransactionPeriodId): Response {
  return new Response(JSON.stringify(transactionView(periodId)), { status: 200 });
}

function spendView(): SpendView {
  return {
    generated_at: "2026-01-01T00:00:00Z",
    cards: [],
  } satisfies SpendView;
}

let root: Root | null = null;
let container: HTMLDivElement;

async function mount(activeTab: SpendTab, transactionPeriodId: TransactionPeriodId): Promise<void> {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root?.render(<Probe activeTab={activeTab} transactionPeriodId={transactionPeriodId} />));
}

async function rerender(activeTab: SpendTab, transactionPeriodId: TransactionPeriodId): Promise<void> {
  await act(async () => root?.render(<Probe activeTab={activeTab} transactionPeriodId={transactionPeriodId} />));
}

async function unmount(): Promise<void> {
  await act(async () => root?.unmount());
  root = null;
}

function outputText(): string {
  return container.querySelector("output")?.textContent ?? "";
}

beforeEach(() => {
  TestEventSource.instances = [];
  vi.stubGlobal("EventSource", TestEventSource);
});

afterEach(async () => {
  if (root) await unmount();
  container?.remove();
  vi.unstubAllGlobals();
});

it("returns to live state when the EventSource reconnects and delivers a view", async () => {
  await mount("sustainability", "rolling_30d");
  const stream = TestEventSource.instances[0];
  if (!stream) throw new Error("EventSource was not created");

  stream.open();
  await act(async () => stream.sendView("{"));
  expect(outputText()).not.toContain("|no-error|");

  await act(async () => stream.failWhileConnecting());
  expect(outputText()).toMatch(/^Reconnecting\|/);

  await act(async () => {
    stream.open();
    stream.sendView(spendView());
  });
  expect(outputText()).toBe("Live updates|view|no-error|no-period|loaded|no-error-period|no-tx-error");
});

it("reports a terminal first-load failure without claiming a view was loaded", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(new Response("unavailable", { status: 503 })))
  );
  await mount("sustainability", "rolling_30d");
  const stream = TestEventSource.instances[0];
  if (!stream) throw new Error("EventSource was not created");

  await act(async () => {
    stream.readyState = TestEventSource.CLOSED;
    stream.onerror?.(new Event("error"));
    await Promise.resolve();
    await Promise.resolve();
  });

  expect(outputText()).toBe(
    "Unable to connect|no-view|Connection check returned 503|no-period|loaded|no-error-period|no-tx-error"
  );
});

it("loads an HTTP view snapshot after the live stream closes terminally", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(new Response(JSON.stringify(spendView()), { status: 200 })))
  );
  await mount("sustainability", "rolling_30d");
  const stream = TestEventSource.instances[0];
  if (!stream) throw new Error("EventSource was not created");

  await act(async () => {
    stream.readyState = TestEventSource.CLOSED;
    stream.onerror?.(new Event("error"));
    await Promise.resolve();
    await Promise.resolve();
  });

  expect(outputText()).toBe("Unable to connect|view|no-error|no-period|loaded|no-error-period|no-tx-error");
});

it("aborts and ignores transaction data for a period that was replaced", async () => {
  const oldResponse = deferred<Response>();
  const newResponse = deferred<Response>();
  const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
    const period = new URL(String(input), "http://test.invalid").searchParams.get("period");
    return period === "rolling_30d" ? oldResponse.promise : newResponse.promise;
  });
  vi.stubGlobal("fetch", fetchMock);

  await mount("transactions", "rolling_30d");
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(String(fetchMock.mock.calls[0]?.[0])).toBe("/api/v1/transactions?period=rolling_30d");
  const oldSignal = fetchMock.mock.calls[0]?.[1]?.signal;

  await rerender("transactions", "credit_cycle");
  expect(oldSignal?.aborted).toBe(true);
  expect(fetchMock).toHaveBeenCalledTimes(2);
  expect(String(fetchMock.mock.calls[1]?.[0])).toBe("/api/v1/transactions?period=credit_cycle");

  await act(async () => {
    oldResponse.resolve(transactionResponse("rolling_30d"));
    await Promise.resolve();
  });
  expect(outputText()).toContain("no-period|loading");

  await act(async () => {
    newResponse.resolve(transactionResponse("credit_cycle"));
    await Promise.resolve();
  });
  expect(outputText()).toBe("Connecting|no-view|no-error|credit_cycle|loaded|no-error-period|no-tx-error");
});

it("retains matching data when a same-period refresh fails and scopes the error to that period", async () => {
  const refreshResponse = deferred<Response>();
  let requestCount = 0;
  const fetchMock = vi.fn(() => {
    requestCount += 1;
    return requestCount === 1 ? Promise.resolve(transactionResponse("rolling_30d")) : refreshResponse.promise;
  });
  vi.stubGlobal("fetch", fetchMock);

  await mount("transactions", "rolling_30d");
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(outputText()).toContain("no-error|rolling_30d|loaded|no-error-period|no-tx-error");

  const stream = TestEventSource.instances[0];
  if (!stream) throw new Error("EventSource was not created");
  await act(async () => stream.sendView(spendView()));
  expect(outputText()).toContain("view|no-error|rolling_30d|loading|no-error-period");

  await act(async () => {
    refreshResponse.resolve(new Response("unavailable", { status: 503 }));
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(outputText()).toContain("view|no-error|rolling_30d|loaded|rolling_30d|Transactions request returned 503");
});

it("keeps the prior period in state but associates a failed switch with its requested period", async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const period = new URL(String(input), "http://test.invalid").searchParams.get("period");
    return Promise.resolve(
      period === "rolling_30d" ? transactionResponse("rolling_30d") : new Response("unavailable", { status: 503 })
    );
  });
  vi.stubGlobal("fetch", fetchMock);

  await mount("transactions", "rolling_30d");
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  await rerender("transactions", "credit_cycle");
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });

  expect(outputText()).toContain("no-view|no-error|rolling_30d|loaded|credit_cycle|Transactions request returned 503");
});
