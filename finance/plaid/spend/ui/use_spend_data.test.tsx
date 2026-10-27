// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { pydanticWireJson } from "./pydantic_wire_fixtures.generated";
import { useSpendData } from "./use_spend_data";
import {
  decodeSpendConfiguration,
  decodeSpendTransactions,
  decodeSpendView,
  decodeSustainabilityDashboard,
} from "./wire";
import type { SpendTab, TransactionPeriodId, TransactionsView } from "./types";

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
    <output
      data-configuration-error={spend.configurationError ?? ""}
      data-transactions-error={spend.transactionsError ?? ""}
      data-view-label={spend.view?.cards[0]?.label ?? "no-view"}
    >
      {`${spend.connectionState}|${spend.error ?? "no-error"}|${spend.transactions?.requested_period_id ?? "no-period"}|${spend.transactionsLoading ? "loading" : "loaded"}`}
    </output>
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

function transactionView(periodId: TransactionPeriodId): TransactionsView {
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
  } satisfies TransactionsView;
}

function transactionResponse(periodId: TransactionPeriodId): Response {
  return new Response(JSON.stringify(transactionView(periodId)), { status: 200 });
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
    stream.sendView(pydanticWireJson.spendView);
  });
  expect(outputText()).toBe("Live updates|no-error|no-period|loaded");
  expect(container.querySelector("output")?.dataset.viewLabel).toBe("Everyday");

  await act(async () => stream.sendView(JSON.stringify({ generated_at: "not-a-date", cards: [] })));
  expect(outputText()).toMatch(/^Invalid update\|Invalid Spend view/);
  expect(container.querySelector("output")?.dataset.viewLabel).toBe("Everyday");

  await act(async () => stream.sendView(pydanticWireJson.spendView));
  expect(outputText()).toBe("Live updates|no-error|no-period|loaded");
  expect(container.querySelector("output")?.dataset.viewLabel).toBe("Everyday");
});

it("decodes exact Pydantic JSON for nested views, null/default fields, and UTC datetime encodings", () => {
  const utcOffsetSpendView = pydanticWireJson.spendView.replace(/Z/g, "+00:00");
  expect(pydanticWireJson.spendView).toContain("2026-10-27T12:00:00Z");
  expect(utcOffsetSpendView).toContain("2026-10-27T12:00:00+00:00");
  expect(decodeSpendView(utcOffsetSpendView)).toMatchObject({
    generated_at: "2026-10-27T12:00:00+00:00",
    cards: [{ label: "Everyday", statement_period: { kind: "statement" }, pending_minor_units: 345 }],
    allowance: null,
    dashboard_url: null,
  });
  expect(decodeSpendView(pydanticWireJson.spendView)).toMatchObject({
    generated_at: "2026-10-27T12:00:00Z",
    cards: [{ label: "Everyday", statement_period: { kind: "statement" }, pending_minor_units: 345 }],
    allowance: null,
    dashboard_url: null,
  });
  expect(decodeSpendConfiguration(pydanticWireJson.spendConfiguration)).toMatchObject({
    time_zone: "UTC",
    cards: [{ label: "Everyday", limit_minor_units: 500000 }],
    allowance: null,
  });
  expect(decodeSpendTransactions(pydanticWireJson.spendTransactions)).toMatchObject({
    requested_period_id: "rolling_30d",
    period: { start: "2026-09-28", end: "2026-10-27" },
    summary: { transaction_count: 1, net_allowance_spend_minor_units: 12345 },
    allowance: null,
  });
  expect(decodeSustainabilityDashboard(pydanticWireJson.sustainabilityDashboard)).toMatchObject({
    default_window_days: 90,
    windows: [{ status: "disabled", portfolio: { status: "unavailable", sources: [] } }],
  });
});

it.each(["{", JSON.stringify({ time_zone: "UTC", cards: [{ label: "partial" }] })])(
  "reports malformed configuration responses without treating them as valid (%s)",
  async (body) => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(body, { status: 200 }))
    );
    await mount("configuration", "rolling_30d");
    await vi.waitFor(() =>
      expect(container.querySelector("output")?.dataset.configurationError).toMatch(/^Invalid Spend configuration/)
    );
  }
);

it.each(["{", JSON.stringify({ requested_period_id: "rolling_30d", rows: [] })])(
  "reports malformed transaction responses without treating them as valid (%s)",
  async (body) => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(body, { status: 200 }))
    );
    await mount("transactions", "rolling_30d");
    await vi.waitFor(() =>
      expect(container.querySelector("output")?.dataset.transactionsError).toMatch(/^Invalid Spend transactions/)
    );
  }
);

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
  expect(outputText()).toBe("Connecting|no-error|credit_cycle|loaded");
});
