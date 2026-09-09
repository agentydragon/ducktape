// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { ConnectionRequestError, type ConnectionService } from "./client";
import { Connections } from "./connections";
import { sampleConnection } from "./connections_fixture";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];
afterEach(async () => {
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
});
async function render(service: ConnectionService): Promise<HTMLDivElement> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () =>
    root.render(
      <MantineProvider>
        <Connections service={service} />
      </MantineProvider>
    )
  );
  return container;
}
function button(container: HTMLElement, name: string): HTMLButtonElement {
  const found = [...container.querySelectorAll("button")].find((node) => node.textContent === name);
  if (!found) throw new Error(`Missing ${name}`);
  return found;
}
function service(): ConnectionService {
  return {
    list: vi.fn(async () => [sampleConnection()]),
    identities: vi.fn(async () => ({ personal: { enabled: false } })),
    rename: vi.fn(),
    unbind: vi.fn(),
  };
}
it("renders immutable IDs and distinguishes disabled Identity from active grant", async () => {
  const container = await render(service());
  for (const value of [
    "Claude desktop",
    sampleConnection().id,
    "registered-client-123",
    "Grant active",
    "personal",
    "disabled",
  ])
    expect(container.textContent).toContain(value);
});
it("requires confirmation, permits cancelling, and preserves the row after unbind", async () => {
  const api = service();
  const row = sampleConnection();
  api.unbind = vi.fn(async () => ({
    ...row,
    version: 3,
    grants: row.grants.map((grant) => ({ ...grant, status: "revoked" as const })),
  }));
  const container = await render(api);
  await act(async () => button(container, "Unbind").click());
  expect(api.unbind).not.toHaveBeenCalled();
  expect(container.textContent).toContain("Already claimed executions are not stopped");
  await act(async () => button(container, "Cancel").click());
  expect(api.unbind).not.toHaveBeenCalled();
  await act(async () => button(container, "Unbind").click());
  await act(async () => button(container, "Confirm unbind").click());
  expect(api.unbind).toHaveBeenCalledOnce();
  expect(api.unbind).toHaveBeenCalledWith(row);
  expect(container.textContent).toContain("Grant revoked");
  expect(container.textContent).toContain(row.id);
  expect(button(container, "Unbind").disabled).toBe(true);
});
it("sends a trimmed rename with the displayed version", async () => {
  const api = service();
  const row = sampleConnection();
  api.rename = vi.fn(async () => ({ ...row, display_name: "Renamed", version: 3 }));
  const container = await render(api);
  await act(async () => button(container, "Rename").click());
  const input = container.querySelector("input");
  if (!input) throw new Error("missing name input");
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, " Renamed ");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => button(container, "Save name").click());
  expect(api.rename).toHaveBeenCalledOnce();
  expect(api.rename).toHaveBeenCalledWith(row, "Renamed");
  expect(container.textContent).toContain("Renamed");
});
it("refreshes stale unbind state without destructive automatic retry", async () => {
  const api = service();
  let rows = [sampleConnection()];
  api.list = vi.fn(async () => rows);
  api.unbind = vi.fn(async () => {
    rows = [{ ...rows[0], display_name: "Changed elsewhere", version: 4 }];
    throw new ConnectionRequestError(409, "stale");
  });
  const container = await render(api);
  await act(async () => button(container, "Unbind").click());
  await act(async () => button(container, "Confirm unbind").click());
  expect(api.unbind).toHaveBeenCalledOnce();
  expect(api.list).toHaveBeenCalledTimes(2);
  expect(container.textContent).toContain("Changed elsewhere");
  expect(container.textContent).toContain("Nothing was retried");
  expect(container.textContent).not.toContain("Confirm unbind");
});
it("distinguishes loading failures from an empty inventory", async () => {
  const api = service();
  api.list = vi.fn(async () => {
    throw new Error("Service unavailable");
  });
  const container = await render(api);
  expect(container.textContent).toContain("Service unavailable");
  expect(container.textContent).not.toContain("No Connections yet");
});
