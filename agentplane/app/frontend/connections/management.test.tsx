// @vitest-environment happy-dom
import { act } from "react";
import { MemoryRouter } from "react-router";
import { expect, it, vi } from "vitest";
import { create, toJson, type JsonObject } from "@bufbuild/protobuf";

import { ConnectionSchema, GrantSchema, GrantStatus, type Connection } from "../../../mcp_edge/connection_service_pb";
import { IdentitySchema, ServiceAccountSchema } from "../../../proto/identity_pb";
import { answerConnectionFacade, mountConnectionService, type Sent } from "./testing/test_support";
import { Connections } from "./management";
import { sampleConnection } from "./testing/fixture";

vi.mock("./caller_grants", () => ({
  CallerGrants: ({ identity }: { identity: { name: string; uid: string } }) => <div>Grants for {identity.name}</div>,
}));

const OTHER = create(IdentitySchema, { name: "other", uid: "30000000-0000-4000-8000-000000000002" });
const ROOT = "/upstream/connections/v1/operator";
const CONNECTIONS = `${ROOT}/connections`;
const CALLERS = `${ROOT}/connection-identities`;
const WATCH = `${ROOT}/connections:watch`;

function connectionJson(row: Connection): JsonObject {
  return toJson(ConnectionSchema, row) as JsonObject;
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
    if (method === "GET" && path === CALLERS) {
      return { identities: [toJson(IdentitySchema, OTHER)] };
    }
    if (method === "POST" && path.endsWith("/rebind")) {
      return { connection: connectionJson(create(ConnectionSchema, { ...sampleConnection(), boundIdentity: OTHER })) };
    }
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

async function render(): Promise<HTMLDivElement> {
  const container = await mountConnectionService(
    <MemoryRouter>
      <Connections />
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

async function chooseIdentity(container: HTMLElement): Promise<void> {
  const input = container.querySelector<HTMLInputElement>('input[aria-label="Identity for Claude desktop"]');
  if (!input) throw new Error("Missing Identity picker");
  await act(async () => input.click());
  const listId = input.getAttribute("aria-controls");
  const list = listId ? document.getElementById(listId) : null;
  const option = [...(list?.querySelectorAll<HTMLElement>('[role="option"]') ?? [])].find(
    (candidate) => candidate.textContent === `${OTHER.name} · UID ${OTHER.uid}`
  );
  if (!option) throw new Error("Missing live Identity option");
  await act(async () => option.click());
}
it("renders the Client/Identity table with the immutable client ID and a missing Identity flagged", async () => {
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
  expect(container.textContent).toContain("Identity");
  expect(nameField(container).value).toBe("Claude desktop");
  for (const value of ["registered-client-123", sampleConnection().id, "personal"])
    expect(container.textContent).toContain(value);
  expect(container.textContent).toContain("Bound Identity is missing, deleting, or has been replaced");
});
it("shows the most recent grant when a connection has more than one", async () => {
  const row = sampleConnection();
  const superseded = row.grants[0];
  if (!superseded) throw new Error("missing sample grant");
  const multiple = create(ConnectionSchema, {
    ...row,
    grants: [
      superseded,
      create(GrantSchema, {
        ...superseded,
        id: "20000000-0000-4000-8000-000000000002",
        revision: superseded.revision + 1,
        clientId: "registered-client-456",
        identity: OTHER,
      }),
    ],
    boundIdentity: OTHER,
  });
  serve([multiple]);
  const container = await render();
  expect(container.textContent).toContain("registered-client-456");
  expect(container.textContent).not.toContain("registered-client-123");
  expect(container.textContent).toContain("other");
  expect(container.textContent).not.toContain("Bound Identity is missing, deleting, or has been replaced");
});
it("requires confirmation, permits cancelling, and preserves the row after unlink", async () => {
  const row = create(ConnectionSchema, { ...sampleConnection(), version: 9007199254740993n });
  const sent = serve([row], {
    unbound: () =>
      create(ConnectionSchema, {
        ...row,
        version: 3n,
        grants: row.grants.map((grant) => create(GrantSchema, { ...grant, status: GrantStatus.REVOKED })),
        boundIdentity: undefined,
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
    { path: `${CONNECTIONS}/${row.id}/unbind`, body: { expectedVersion: row.version.toString() } },
  ]);
});
it("refreshes stale unlink state without destructive automatic retry", async () => {
  let rows = [sampleConnection()];
  const sent = serve(() => rows, {
    unbound: () => {
      rows = [create(ConnectionSchema, { ...rows[0], displayName: "Changed elsewhere", version: 4n })];
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
    renamed: () => create(ConnectionSchema, { ...row, displayName: "Test renamed client", version: 3n }),
    unbound: () => row,
  });
  const container = await render();

  await type(nameField(container), "  Test renamed client  ");
  await pressEnter(nameField(container));

  await vi.waitFor(() => expect(nameField(container).value).toBe("Test renamed client"));
  expect(renames(sent)).toMatchObject([
    {
      path: `${CONNECTIONS}/${row.id}`,
      body: { expectedVersion: row.version.toString(), displayName: "Test renamed client" },
    },
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
      rows = [create(ConnectionSchema, { ...rows[0], displayName: "Test renamed elsewhere", version: 4n })];
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

it("expands grants for the exact live bound Identity", async () => {
  serve([create(ConnectionSchema, { ...sampleConnection(), boundIdentity: OTHER })]);
  const container = await render();
  await act(async () => button(container, "View grants").click());
  expect(container.textContent).toContain("Grants for other");
  await act(async () => button(container, "Hide grants").click());
  expect(container.textContent).not.toContain("Grants for other");
});

it.each([
  {
    label: "a paused legacy Connection",
    row: create(ConnectionSchema, {
      ...sampleConnection(),
      boundIdentity: undefined,
      identityBindingRequired: true,
      legacyBoundCaller: create(ServiceAccountSchema, { namespace: "legacy", name: "other" }),
    }),
  },
  {
    label: "a same-name replacement with a different UID",
    row: create(ConnectionSchema, {
      ...sampleConnection(),
      boundIdentity: create(IdentitySchema, { name: OTHER.name, uid: "retained-identity-uid" }),
    }),
  },
])("requires explicit confirmation before rebinding $label", async ({ row }) => {
  const sent = serve([row]);
  const container = await render();
  expect(container.textContent).not.toContain("View grants");
  const rebinds = (): Sent[] => sent.filter(({ path }) => path.endsWith("/rebind"));
  await chooseIdentity(container);
  expect(rebinds()).toEqual([]);
  await act(async () => button(container, "Apply").click());
  expect(rebinds()).toEqual([]);
  await act(async () => button(container, "Confirm change").click());
  await vi.waitFor(() => expect(rebinds()).toHaveLength(1));
  expect(rebinds()).toMatchObject([
    {
      path: `${CONNECTIONS}/${row.id}/rebind`,
      body: { expectedVersion: row.version.toString(), identity: { name: OTHER.name, uid: OTHER.uid } },
    },
  ]);
  await vi.waitFor(() => expect(container.textContent).toContain("View grants"));
});
