// @vitest-environment happy-dom
import { act } from "react";
import { MemoryRouter } from "react-router";
import { expect, it, vi } from "vitest";

import type { CallerGrantReader, Connection } from "./models";
import { answerConnectionFacade, mountConnectionService, type Sent } from "./testing/test_support";
import { Connections } from "./management";
import { sampleConnection } from "./testing/fixture";

const OTHER = { namespace: "agentplane-test", name: "other" };
const ROOT = "/upstream/connections/v1/operator";
const CONNECTIONS = `${ROOT}/connections`;
const CALLERS = `${ROOT}/caller-service-accounts`;
const WATCH = `${ROOT}/connections:watch`;

function connectionJson(row: Connection): Record<string, unknown> {
  const status = {
    pending: "GRANT_STATUS_PENDING",
    active: "GRANT_STATUS_ACTIVE",
    revoked: "GRANT_STATUS_REVOKED",
  } as const;
  return {
    id: row.id,
    displayName: row.display_name,
    version: row.version,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    grants: row.grants.map((grant) => ({
      id: grant.id,
      connectionId: grant.connection_id,
      revision: grant.revision,
      caller: grant.caller,
      issuer: grant.issuer,
      clientId: grant.client_id,
      status: status[grant.status],
      createdAt: grant.created_at,
      ...(grant.activated_at === null ? {} : { activatedAt: grant.activated_at }),
      ...(grant.revoked_at === null ? {} : { revokedAt: grant.revoked_at }),
    })),
    ...(row.bound_caller === null ? {} : { boundCaller: row.bound_caller }),
  };
}

function watch(rows: Connection[], signal?: AbortSignal): Response {
  const encoder = new TextEncoder();
  const frames = [{ snapshot: { connections: rows.map(connectionJson) } }, { snapshotComplete: {} }];
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const result of frames) controller.enqueue(encoder.encode(`${JSON.stringify({ result })}\n`));
        signal?.addEventListener(
          "abort",
          () => controller.error(new DOMException("The watch request was aborted", "AbortError")),
          { once: true }
        );
      },
    }),
    { headers: { "content-type": "application/x-ndjson" } }
  );
}

/** Answers the page's reads with `rows` (a function, to change between reads), an unlink with
 * `unbound` and a rename with `renamed`; a `Response` answers as an HTTP failure. */
function serve(
  rows: Connection[] | Response | (() => Connection[] | Response) = [sampleConnection()],
  { unbound, renamed }: { unbound?: () => Connection | Response; renamed?: () => Connection | Response } = {}
): Sent[] {
  const sent = answerConnectionFacade(({ method, path }, request) => {
    if (method === "GET" && path === CONNECTIONS) {
      const current = typeof rows === "function" ? rows() : rows;
      return current instanceof Response ? current : { connections: current.map(connectionJson) };
    }
    if (method === "GET" && path === WATCH) {
      const current = typeof rows === "function" ? rows() : rows;
      return current instanceof Response ? current : watch(current, request.signal);
    }
    if (method === "GET" && path === CALLERS) return { serviceAccounts: [OTHER] };
    if (method === "POST" && path.endsWith("/rebind")) return { connection: connectionJson(sampleConnection()) };
    if (method === "POST" && path.endsWith("/unbind")) {
      const answer = unbound?.();
      return answer instanceof Response || answer === undefined ? answer : { connection: connectionJson(answer) };
    }
    if (method === "PATCH") {
      const answer = renamed?.();
      return answer instanceof Response || answer === undefined ? answer : { connection: connectionJson(answer) };
    }
  });
  return sent;
}

const reads = (sent: Sent[]): number => sent.filter(({ path }) => path === CONNECTIONS).length;
const unbinds = (sent: Sent[]): Sent[] => sent.filter(({ path }) => path.endsWith("/unbind"));
const renames = (sent: Sent[]): Sent[] => sent.filter(({ method }) => method === "PATCH");

async function render(readGrants?: CallerGrantReader): Promise<HTMLDivElement> {
  const container = await mountConnectionService(
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
function nameField(container: HTMLElement): HTMLInputElement {
  const found = container.querySelector<HTMLInputElement>('input[aria-label="Client name"]');
  if (!found) throw new Error("Missing Client name");
  return found;
}
async function type(input: HTMLInputElement, text: string): Promise<void> {
  await act(async () => {
    input.focus();
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, text);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function pressEnter(input: HTMLInputElement): Promise<void> {
  await act(async () => {
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  });
}
it("renders the Client/Service account table with the immutable client ID and an unlabeled caller flagged", async () => {
  const sent = serve();
  const container = await render();
  expect(sent).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ method: "GET", path: CONNECTIONS }),
      expect.objectContaining({ method: "GET", path: CALLERS }),
      expect.objectContaining({ method: "GET", path: WATCH }),
    ])
  );
  expect(container.textContent).toContain("Client");
  expect(container.textContent).toContain("Service account");
  expect(nameField(container).value).toBe("Claude desktop");
  for (const value of ["registered-client-123", sampleConnection().id, "agentplane-test/personal"])
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
  row.version = "9007199254740993";
  const sent = serve([row], {
    unbound: () => ({
      ...row,
      version: "3",
      grants: row.grants.map((grant) => ({ ...grant, status: "revoked" as const })),
      bound_caller: null,
    }),
  });
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
    { path: `${CONNECTIONS}/${row.id}/unbind`, body: { expectedVersion: row.version } },
  ]);
});
it("refreshes stale unlink state without destructive automatic retry", async () => {
  let rows = [sampleConnection()];
  const sent = serve(() => rows, {
    unbound: () => {
      rows = [{ ...rows[0], display_name: "Changed elsewhere", version: "4" }];
      return Response.json({ detail: "stale" }, { status: 409 });
    },
  });
  const container = await render();
  await act(async () => button(container, "Unlink").click());
  await act(async () => button(container, "Confirm unlink").click());
  await vi.waitFor(() => expect(nameField(container).value).toBe("Changed elsewhere"));
  expect(unbinds(sent)).toHaveLength(1);
  expect(reads(sent)).toBe(2);
  expect(container.textContent).toContain("Nothing was retried");
  expect(container.textContent).not.toContain("Confirm unlink");
});
it("renames with the version it read and keeps the answered row", async () => {
  const row = sampleConnection();
  const sent = serve([row], {
    renamed: () => ({ ...row, display_name: "Test renamed client", version: "3" }),
    unbound: () => row,
  });
  const container = await render();

  await type(nameField(container), "  Test renamed client  ");
  await pressEnter(nameField(container));

  await vi.waitFor(() => expect(nameField(container).value).toBe("Test renamed client"));
  expect(renames(sent)).toMatchObject([
    { path: `${CONNECTIONS}/${row.id}`, body: { expectedVersion: row.version, displayName: "Test renamed client" } },
  ]);
  // The next change on the row goes with the version the rename answered, not the one first read.
  await act(async () => button(container, "Unlink").click());
  await act(async () => button(container, "Confirm unlink").click());
  await vi.waitFor(() => expect(unbinds(sent)).toMatchObject([{ body: { expectedVersion: "3" } }]));
});
it("keeps the stored name when a blank one is committed", async () => {
  const sent = serve();
  const container = await render();

  await type(nameField(container), "   ");
  await act(async () => nameField(container).blur());

  expect(nameField(container).value).toBe("Claude desktop");
  // Not locked for a save in flight: nothing was sent.
  expect(nameField(container).disabled).toBe(false);
  expect(renames(sent)).toEqual([]);
});
it("loads the latest state on a rename conflict without retrying", async () => {
  let rows = [sampleConnection()];
  const sent = serve(() => rows, {
    renamed: () => {
      rows = [{ ...rows[0], display_name: "Test renamed elsewhere", version: "4" }];
      return Response.json({ detail: "stale" }, { status: 409 });
    },
  });
  const container = await render();

  await type(nameField(container), "Test typed name");
  await pressEnter(nameField(container));

  await vi.waitFor(() =>
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("Nothing was retried")
  );
  expect(nameField(container).value).toBe("Test renamed elsewhere");
  expect(renames(sent)).toHaveLength(1);
  expect(reads(sent)).toBe(2);
});
it("distinguishes loading failures from an empty inventory", async () => {
  serve(() => Response.json({ detail: "Service unavailable" }, { status: 503 }));
  const container = await render();
  expect(container.textContent).toContain("Service unavailable");
  expect(container.textContent).not.toContain("No OAuth clients yet");
});

it("expands grants for the current caller even when its admission label is missing", async () => {
  const read = vi.fn<CallerGrantReader>(async () => ({
    bindings: [],
    action_policy: { synced: true, auto_approve_if: [] },
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
