// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { CoverageResponse, OverviewResponse } from "../lib/schema";
import { fetchCoverage } from "../lib/api/client";
import { RunModalContext } from "../lib/runModalContext";
import { toast } from "../lib/toast";
import OverviewPage from "./OverviewPage";

vi.mock("../lib/api/client", () => ({ fetchCoverage: vi.fn(), fetchOverview: vi.fn() }));
vi.mock("../lib/toast", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
vi.mock("../components/stats/CoverageHeatmap", () => ({ default: () => <div data-testid="coverage-heatmap" /> }));
vi.mock("../components/stats/DefinitionsTable", () => ({ default: () => <div /> }));
vi.mock("../components/stats/SummaryCards", () => ({ default: () => <div /> }));
vi.mock("../components/stats/DistributionChart", () => ({
  default: ({ title, values }: { title: string; values: number[] }) => (
    <div data-testid="distribution-chart" data-title={title}>
      {values.join(",")}
    </div>
  ),
}));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function coverage(maxRecall: number): CoverageResponse {
  return {
    examples: [],
    definitions: [],
    cells: [],
    max_recall_values: [maxRecall],
    tp_count_values: [],
  };
}

const initialOverview = { definitions: [], example_counts: {} } as OverviewResponse;

let root: Root | null = null;
let container: HTMLDivElement;

async function mount(): Promise<void> {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(
      <MantineProvider env="test">
        <MemoryRouter>
          <RunModalContext.Provider value={{ open: () => undefined }}>
            <OverviewPage initialData={initialOverview} />
          </RunModalContext.Provider>
        </MemoryRouter>
      </MantineProvider>
    );
  });
}

async function selectSplit(split: "valid" | "train"): Promise<void> {
  const tab = Array.from(container.querySelectorAll('[role="tab"]')).find(
    (candidate) => candidate.textContent?.trim() === (split === "valid" ? "Valid" : "Train")
  );
  if (!(tab instanceof HTMLElement)) throw new Error(`Missing ${split} tab`);
  await act(async () => tab.click());
}

async function unmount(): Promise<void> {
  await act(async () => root?.unmount());
  root = null;
}

beforeEach(() => {
  vi.resetAllMocks();
});

afterEach(async () => {
  if (root) await unmount();
  container?.remove();
});

it("clears valid coverage while train loads and after the train request fails", async () => {
  const train = deferred<CoverageResponse>();
  vi.mocked(fetchCoverage).mockImplementation((split) =>
    split === "valid" ? Promise.resolve(coverage(0.25)) : train.promise
  );

  await mount();
  expect(container.querySelector('[data-testid="distribution-chart"]')?.textContent).toBe("0.25");

  await selectSplit("train");
  expect(container.textContent).toContain("Loading analysis...");
  expect(container.querySelector('[data-testid="distribution-chart"]')).toBeNull();

  await act(async () => {
    train.reject(new Error("train coverage unavailable"));
    await train.promise.catch(() => undefined);
  });

  expect(toast.error).toHaveBeenCalledWith("train coverage unavailable");
  expect(container.querySelector('[data-testid="distribution-chart"]')).toBeNull();
  expect(container.textContent).not.toContain("Loading analysis...");
});

it("ignores older coverage requests when split changes rapidly", async () => {
  const train = deferred<CoverageResponse>();
  const refreshedValid = deferred<CoverageResponse>();
  vi.mocked(fetchCoverage)
    .mockResolvedValueOnce(coverage(0.25))
    .mockImplementationOnce(() => train.promise)
    .mockImplementationOnce(() => refreshedValid.promise);

  await mount();
  await selectSplit("train");
  expect(container.textContent).toContain("Loading analysis...");

  await selectSplit("valid");
  expect(container.textContent).toContain("Loading analysis...");
  expect(container.querySelector('[data-testid="distribution-chart"]')).toBeNull();

  await act(async () => {
    train.resolve(coverage(0.9));
    await train.promise;
  });
  expect(container.textContent).toContain("Loading analysis...");
  expect(container.querySelector('[data-testid="distribution-chart"]')).toBeNull();

  await act(async () => {
    refreshedValid.resolve(coverage(0.4));
    await refreshedValid.promise;
  });
  expect(container.querySelector('[data-testid="distribution-chart"]')?.textContent).toBe("0.4");
  expect(container.textContent).not.toContain("Loading analysis...");
});
