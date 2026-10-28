// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { ComponentStatus } from "./api/schema";
import { api } from "./client";
import { SystemStatus } from "./system";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];
beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(async () => {
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
  vi.useRealTimers();
  vi.restoreAllMocks();
});

const HEALTHY: ComponentStatus = {
  name: "Healthy component",
  state: "ok",
  detail: null,
  checked_at: "2026-01-01T00:00:00Z",
};
const DOWN: ComponentStatus = {
  name: "Down component",
  state: "down",
  detail: "UNAVAILABLE: test connection refused",
  checked_at: "2026-01-01T00:00:00Z",
};

async function render(): Promise<HTMLDivElement> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () =>
    root.render(
      <MantineProvider>
        <SystemStatus />
      </MantineProvider>
    )
  );
  return container;
}

/** `api.GET` answering its successive calls with the given lists. */
function answer(...lists: ComponentStatus[][]) {
  const get = vi.spyOn(api, "GET");
  for (const data of lists) get.mockResolvedValueOnce({ data, response: new Response() } as never);
  return get;
}

function rowOf(container: HTMLElement, name: string): HTMLElement {
  const row = [...container.querySelectorAll("tbody tr")].find((candidate) => candidate.textContent?.includes(name));
  if (!(row instanceof HTMLElement)) throw new Error(`no row for ${name}`);
  return row;
}

it("lists each component with its state and why it is down", async () => {
  answer([HEALTHY, DOWN]);
  const container = await render();
  expect(rowOf(container, HEALTHY.name).textContent).toContain("ok");
  const down = rowOf(container, DOWN.name).textContent;
  expect(down).toContain("down");
  expect(down).toContain(DOWN.detail);
});

it("asks again every 15 seconds and shows the newer answer", async () => {
  const get = answer([DOWN], [HEALTHY]);
  const container = await render();
  expect(rowOf(container, DOWN.name)).toBeDefined();
  await act(async () => vi.advanceTimersByTimeAsync(15_000));
  expect(get).toHaveBeenCalledTimes(2);
  expect(rowOf(container, HEALTHY.name)).toBeDefined();
  expect(container.textContent).not.toContain(DOWN.name);
});

it("reports a failed read instead of an empty list", async () => {
  vi.spyOn(api, "GET").mockResolvedValue({
    error: { detail: "test upstream unavailable" },
    response: new Response(null, { status: 503 }),
  } as never);
  const container = await render();
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("test upstream unavailable");
});
