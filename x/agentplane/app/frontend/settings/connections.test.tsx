// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { ConnectionRequestError, type ConnectionService } from "../client";
import { Connections } from "./connections";
import { sampleConnection } from "../connections_fixture";

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
    callerServiceAccounts: vi.fn(async () => [{ namespace: "agentplane-test", name: "other" }]),
    rename: vi.fn(),
    unbind: vi.fn(),
  };
}
it("renders the Client/Service account table with the immutable client ID and an unlabeled caller flagged", async () => {
  const container = await render(service());
  expect(container.textContent).toContain("Client");
  expect(container.textContent).toContain("Service account");
  for (const value of ["registered-client-123", "Claude desktop", sampleConnection().id, "agentplane-test/personal"])
    expect(container.textContent).toContain(value);
  expect(container.textContent).toContain("not a labeled caller");
});
it("shows the most recent grant when a connection has more than one", async () => {
  const row = sampleConnection();
  const superseded = row.grants[0];
  row.grants = [
    superseded,
    {
      ...superseded,
      id: "20000000-0000-4000-8000-000000000002",
      revision: superseded.revision + 1,
      client_id: "registered-client-456",
      caller: { namespace: "agentplane-test", name: "other" },
    },
  ];
  const api = service();
  api.list = vi.fn(async () => [row]);
  const container = await render(api);
  expect(container.textContent).toContain("registered-client-456");
  expect(container.textContent).not.toContain("registered-client-123");
  expect(container.textContent).toContain("agentplane-test/other");
  expect(container.textContent).not.toContain("not a labeled caller");
});
it("requires confirmation, permits cancelling, and preserves the row after unlink", async () => {
  const api = service();
  const row = sampleConnection();
  api.unbind = vi.fn(async () => ({
    ...row,
    version: 3,
    grants: row.grants.map((grant) => ({ ...grant, status: "revoked" as const })),
  }));
  const container = await render(api);
  await act(async () => button(container, "Unlink").click());
  expect(api.unbind).not.toHaveBeenCalled();
  expect(container.textContent).toContain("Already claimed executions are not stopped");
  await act(async () => button(container, "Cancel").click());
  expect(api.unbind).not.toHaveBeenCalled();
  await act(async () => button(container, "Unlink").click());
  await act(async () => button(container, "Confirm unlink").click());
  expect(api.unbind).toHaveBeenCalledOnce();
  expect(api.unbind).toHaveBeenCalledWith(row);
  expect(button(container, "Unlink").disabled).toBe(true);
});
it("refreshes stale unlink state without destructive automatic retry", async () => {
  const api = service();
  let rows = [sampleConnection()];
  api.list = vi.fn(async () => rows);
  api.unbind = vi.fn(async () => {
    rows = [{ ...rows[0], display_name: "Changed elsewhere", version: 4 }];
    throw new ConnectionRequestError(409, "stale");
  });
  const container = await render(api);
  await act(async () => button(container, "Unlink").click());
  await act(async () => button(container, "Confirm unlink").click());
  expect(api.unbind).toHaveBeenCalledOnce();
  expect(api.list).toHaveBeenCalledTimes(2);
  expect(container.textContent).toContain("Changed elsewhere");
  expect(container.textContent).toContain("Nothing was retried");
  expect(container.textContent).not.toContain("Confirm unlink");
});
it("distinguishes loading failures from an empty inventory", async () => {
  const api = service();
  api.list = vi.fn(async () => {
    throw new Error("Service unavailable");
  });
  const container = await render(api);
  expect(container.textContent).toContain("Service unavailable");
  expect(container.textContent).not.toContain("No OAuth clients yet");
});
