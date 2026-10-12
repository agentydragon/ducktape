// @vitest-environment happy-dom
import { act } from "react";
import { MemoryRouter } from "react-router";
import { expect, it, vi } from "vitest";

import { answerApi, mount, TestEventSource, type Sent } from "../actions/testing";
import type { CallerGrantReader, Connection } from "../client";
import { Connections } from "./connections";
import { sampleConnection } from "../connections_fixture";

const OTHER = { namespace: "agentplane-test", name: "other" };

/** Answers the page's reads with `rows` (a function, to change between reads) and an unlink with
 * `unbound`; a `Response` answers as an HTTP failure. */
function serve(
  rows: Connection[] | Response | (() => Connection[]) = [sampleConnection()],
  unbound?: (connection: Connection) => Connection | Response
): Sent[] {
  return answerApi(({ method, path }) => {
    if (path === "/connections") return typeof rows === "function" ? rows() : rows;
    if (path === "/connection-service-accounts") return [OTHER];
    if (method === "POST" && path.endsWith("/unbind")) return unbound?.(sampleConnection());
  });
}

const reads = (sent: Sent[]): number => sent.filter(({ path }) => path === "/connections").length;
const unbinds = (sent: Sent[]): Sent[] => sent.filter(({ path }) => path.endsWith("/unbind"));

async function render(readGrants?: CallerGrantReader): Promise<HTMLDivElement> {
  vi.stubGlobal("EventSource", TestEventSource);
  const container = await mount(
    <MemoryRouter>
      <Connections readGrants={readGrants} />
    </MemoryRouter>
  );
  await vi.waitFor(() => expect(container.textContent).not.toContain("Loading OAuth clients"));
  return container;
}
function button(container: HTMLElement, name: string): HTMLButtonElement {
  const found = [...container.querySelectorAll("button")].find((node) => node.textContent === name);
  if (!found) throw new Error(`Missing ${name}`);
  return found;
}
it("renders the Client/Service account table with the immutable client ID and an unlabeled caller flagged", async () => {
  serve();
  const container = await render();
  expect(container.textContent).toContain("Client");
  expect(container.textContent).toContain("Service account");
  for (const value of ["registered-client-123", "Claude desktop", sampleConnection().id, "agentplane-test/personal"])
    expect(container.textContent).toContain(value);
  expect(container.textContent).toContain("ServiceAccount not labeled as an Action caller");
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
  row.bound_caller = OTHER;
  serve([row]);
  const container = await render();
  expect(container.textContent).toContain("registered-client-456");
  expect(container.textContent).not.toContain("registered-client-123");
  expect(container.textContent).toContain("agentplane-test/other");
  expect(container.textContent).not.toContain("ServiceAccount not labeled as an Action caller");
});
it("requires confirmation, permits cancelling, and preserves the row after unlink", async () => {
  const row = sampleConnection();
  const sent = serve([row], () => ({
    ...row,
    version: 3,
    grants: row.grants.map((grant) => ({ ...grant, status: "revoked" as const })),
    bound_caller: null,
  }));
  const container = await render();
  await act(async () => button(container, "Unlink").click());
  expect(unbinds(sent)).toEqual([]);
  expect(container.textContent).toContain("Already claimed executions are not stopped");
  expect(container.querySelector(`[data-connection-id="${row.id}"]`)?.textContent).toContain(
    "Already claimed executions are not stopped"
  );
  await act(async () => button(container, "Cancel").click());
  expect(unbinds(sent)).toEqual([]);
  await act(async () => button(container, "Unlink").click());
  await act(async () => button(container, "Confirm unlink").click());
  await vi.waitFor(() => expect(button(container, "Unlink").disabled).toBe(true));
  expect(unbinds(sent)).toMatchObject([
    { path: `/connections/${row.id}/unbind`, body: { expected_version: row.version } },
  ]);
});
it("refreshes stale unlink state without destructive automatic retry", async () => {
  let rows = [sampleConnection()];
  const sent = serve(
    () => rows,
    () => {
      rows = [{ ...rows[0], display_name: "Changed elsewhere", version: 4 }];
      return Response.json({ detail: "stale" }, { status: 409 });
    }
  );
  const container = await render();
  await act(async () => button(container, "Unlink").click());
  await act(async () => button(container, "Confirm unlink").click());
  await vi.waitFor(() => expect(container.textContent).toContain("Changed elsewhere"));
  expect(unbinds(sent)).toHaveLength(1);
  expect(reads(sent)).toBe(2);
  expect(container.textContent).toContain("Nothing was retried");
  expect(container.textContent).not.toContain("Confirm unlink");
});
it("distinguishes loading failures from an empty inventory", async () => {
  serve(Response.json({ detail: "Service unavailable" }, { status: 503 }));
  const container = await render();
  expect(container.textContent).toContain("Service unavailable");
  expect(container.textContent).not.toContain("No OAuth clients yet");
});

it("expands grants for the current caller even when its admission label is missing", async () => {
  const read = vi.fn<CallerGrantReader>(async () => ({
    egress_bindings: [],
    action_policy: { synced: true, bindings: [], auto_approve_if: [] },
  }));
  serve();
  const container = await render(read);
  expect(read).not.toHaveBeenCalled();
  await act(async () => button(container, "View grants").click());
  expect(read.mock.calls[0][0]).toEqual(sampleConnection().bound_caller);
  expect(container.textContent).toContain("Grants for agentplane-test/personal");
  await act(async () => button(container, "Hide grants").click());
  expect(container.textContent).not.toContain("Grants for agentplane-test/personal");
});
