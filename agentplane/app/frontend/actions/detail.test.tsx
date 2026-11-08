// @vitest-environment happy-dom

import { type JSX, act } from "react";
import { MemoryRouter, Route, Routes, useLocation, useParams } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { clone, create, fromJson } from "@bufbuild/protobuf";
import { ValueSchema } from "@bufbuild/protobuf/wkt";
import { useQueryClient } from "@tanstack/react-query";
import { deferred } from "../../../../util/frontend/testing/deferred";

import { ActionRequestDetail } from "./detail";
import {
  ActionIdentitySchema,
  ActionGroupSchema,
  ActionRequestSchema,
  ActionState,
  ExecutorKind,
  type ActionGroup,
  type ActionRequest,
} from "../../../action_service/action_service_pb";
import { ActionsSidebarSection } from "./sidebar";
import { ActionRequestsProvider } from "./requests";
import { answerApi, mount, request, send, TestNdjsonWatch, unmountLast, type Sent } from "./testing/harness";
import { actionRequestUpsertProtoJson } from "./testing/proto_fixtures";

const ROW = request(ActionState.DECISION_PENDING, 21);
ROW.action = create(ActionIdentitySchema, { group: "ssh", name: "exec" });
ROW.arguments = { host: "test-host.example", user: "test-user", command: "echo exact-command" };

function decided(state: ActionState): ActionRequest {
  const row = request(state, 21);
  row.action = ROW.action;
  row.arguments = ROW.arguments;
  row.title = ROW.title;
  return row;
}
/** Answers the detail page's reads: `stored` for the request itself, `decided` for a decision. */
function serve({ stored, decided }: { stored?: ActionRequest; decided?: ActionRequest | Response }): Sent[] {
  return answerApi(({ method, path }) => {
    if (path === "/upstream/actions/v1/action-groups") return [];
    if (method === "GET" && path === `/upstream/actions/v1/action-requests/${ROW.id}`) return stored;
    if (method === "POST" && path === `/upstream/actions/v1/operator/action-requests/${ROW.id}/decision`)
      return decided;
  });
}

const fetched = (sent: Sent[]): boolean =>
  sent.some(({ method, path }) => method === "GET" && path === `/upstream/actions/v1/action-requests/${ROW.id}`);

function DetailRoute(): JSX.Element {
  const { requestId } = useParams();
  if (requestId === undefined) throw new Error("missing requestId");
  return <ActionRequestDetail requestId={requestId} />;
}

function CurrentPath(): JSX.Element {
  const location = useLocation();
  return <div data-testid="current-path">{location.pathname}</div>;
}

function App({ initialPath }: { initialPath: string }): JSX.Element {
  return (
    <MemoryRouter initialEntries={[initialPath]}>
      <ActionRequestsProvider>
        <ActionsSidebarSection />
        <CurrentPath />
        <Routes>
          <Route path="/threads/:threadId" element={<div>Thread contents remain open</div>} />
          <Route path="/actions/:requestId" element={<DetailRoute />} />
          <Route path="/actions" element={<div>All actions</div>} />
        </Routes>
      </ActionRequestsProvider>
    </MemoryRouter>
  );
}

beforeEach(() => {
  sessionStorage.clear();
});

async function openPendingActionDetails(container: HTMLDivElement): Promise<void> {
  await send([ROW]);

  const disclosure = container.querySelector<HTMLButtonElement>(`button[aria-label="Expand ${ROW.title}"]`);
  if (!disclosure) throw new Error("missing pending action disclosure");
  await act(async () => disclosure.click());
  const detailsLink = container.querySelector<HTMLAnchorElement>(`a[aria-label="View details for ${ROW.title}"]`);
  if (!detailsLink) throw new Error("missing action details link");
  await act(async () => detailsLink.click());
}

it("opens cached stream details without a reload and Back returns to the original in-app route", async () => {
  const sent = serve({});
  const container = await mount(<App initialPath="/threads/example-thread" />);
  await openPendingActionDetails(container);

  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe(`/actions/${ROW.id}`);
  expect(fetched(sent)).toBe(false);
  expect(container.textContent).toContain("Exact arguments");
  expect(container.textContent).toContain("exact-command");

  const back = [...container.querySelectorAll("button")].find((candidate) => candidate.textContent?.trim() === "Back");
  if (!back) throw new Error("missing action detail Back button");
  await act(async () => back.click());
  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe("/threads/example-thread");
  expect(container.textContent).toContain("Thread contents remain open");
});

it("renders an Action's human-facing label on the full details page", async () => {
  const row = clone(ActionRequestSchema, ROW);
  row.action = create(ActionIdentitySchema, { group: "kubernetes_admin", name: "pods_list_in_namespace" });
  row.arguments = { namespace: "tofu-controller" };
  serve({ stored: row });
  const container = await mount(<App initialPath={`/actions/${row.id}`} />);

  await vi.waitFor(() => expect(container.textContent).toContain("List pods in namespace tofu-controller"));
  expect(container.textContent).not.toContain("kubernetes_admin / pods_list_in_namespace");
});

it.each([
  { verdict: "allow" as const, resultState: ActionState.ALLOWED, buttonName: "Approve" },
  { verdict: "deny" as const, resultState: ActionState.DENIED, buttonName: "Deny" },
])(
  "returns to the originating page after a successful $verdict decision",
  async ({ verdict, resultState, buttonName }) => {
    const sent = serve({
      decided: decided(resultState),
    });
    const container = await mount(<App initialPath="/threads/example-thread" />);
    await openPendingActionDetails(container);

    const decisionButton = container.querySelector<HTMLButtonElement>(`button[aria-label="${buttonName}"]`);
    if (!decisionButton) throw new Error(`missing ${buttonName} button`);
    await act(async () => decisionButton.click());
    expect(sent).toContainEqual(
      expect.objectContaining({
        method: "POST",
        body: expect.objectContaining({
          verdict: `VERDICT_${verdict.toUpperCase()}`,
          expectedVersion: String(ROW.version),
        }),
      })
    );
    await vi.waitFor(() =>
      expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe("/threads/example-thread")
    );
    expect(container.textContent).toContain("Thread contents remain open");
  }
);

it("stays on action details when a decision fails", async () => {
  serve({ decided: Response.json({ detail: "decision was rejected" }, { status: 409 }) });
  const container = await mount(<App initialPath="/threads/example-thread" />);
  await openPendingActionDetails(container);
  const approve = container.querySelector<HTMLButtonElement>('button[aria-label="Approve"]');
  if (!approve) throw new Error("missing Approve button");

  await act(async () => {
    approve.click();
    await vi.waitFor(() => expect(container.textContent).toContain("decision was rejected"));
  });

  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe(`/actions/${ROW.id}`);
  expect(container.textContent).toContain("Exact arguments");
});

it("keeps a direct action link on its terminal receipt after a successful decision", async () => {
  serve({
    stored: ROW,
    decided: decided(ActionState.ALLOWED),
  });
  const container = await mount(<App initialPath={`/actions/${ROW.id}`} />);
  const approve = await vi.waitFor(() => {
    const button = container.querySelector<HTMLButtonElement>('button[aria-label="Approve"]');
    if (!button) throw new Error("missing Approve button");
    return button;
  });

  await act(async () => approve.click());
  await vi.waitFor(() => expect(container.textContent).toContain("Allowed"));

  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe(`/actions/${ROW.id}`);
  expect(container.textContent).toContain("Action details");
});

it("loads uncached deep links and falls back to the Actions page on Back", async () => {
  const sent = serve({ stored: ROW });
  const container = await mount(<App initialPath={`/actions/${ROW.id}`} />);
  await send([]);
  await vi.waitFor(() => expect(fetched(sent)).toBe(true));
  await vi.waitFor(() => expect(container.textContent).toContain("Exact arguments"));

  const back = [...container.querySelectorAll("button")].find((candidate) => candidate.textContent?.trim() === "Back");
  if (!back) throw new Error("missing action detail Back button");
  await act(async () => back.click());
  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe("/actions");
  expect(container.textContent).toContain("All actions");
});

it("refreshes the durable receipt when a watch snapshot removes an action that was pending", async () => {
  const allowed = decided(ActionState.ALLOWED);
  const sent = serve({ stored: allowed });
  const container = await mount(<App initialPath="/threads/example-thread" />);
  await openPendingActionDetails(container);
  expect(fetched(sent)).toBe(false);

  await send([]);
  await vi.waitFor(() => expect(fetched(sent)).toBe(true));
  await vi.waitFor(() => expect(container.querySelector('button[aria-label="Approve"]')).toBeNull());
  expect(container.querySelector('button[aria-label="Deny"]')).toBeNull();
});

it("shares a pending detail read between observers", async () => {
  const response = deferred<ActionRequest>();
  const sent = answerApi(() => response.promise);
  const container = await mount(
    <MemoryRouter>
      <ActionRequestsProvider>
        <ActionRequestDetail requestId={ROW.id} />
        <ActionRequestDetail requestId={ROW.id} />
      </ActionRequestsProvider>
    </MemoryRouter>
  );
  expect(sent).toHaveLength(1);
  expect(container.querySelectorAll('[role="status"]')).toHaveLength(2);

  await act(async () => response.resolve(ROW));
  await vi.waitFor(() => expect(container.querySelectorAll('button[aria-label="Approve"]')).toHaveLength(2));
  expect(sent).toHaveLength(1);
});

function RefreshCatalog(): JSX.Element {
  const client = useQueryClient();
  return (
    <button onClick={() => void client.invalidateQueries({ queryKey: ["action-groups"] })}>Refresh catalog</button>
  );
}

it("shares metadata reads and applies a refreshed executor catalog to both receipts", async () => {
  const row = decided(ActionState.SUCCEEDED);
  if (row.execution === undefined) throw new Error("missing execution fixture");
  row.execution.result = fromJson(ValueSchema, {
    content: [{ type: "text", text: "test tool failure" }],
    isError: true,
  });
  const initialGroups = deferred<ActionGroup[]>();
  let catalog: ActionGroup[] | Promise<ActionGroup[]> = initialGroups.promise;
  const sent = answerApi(({ path }) => (path.endsWith("/action-groups") ? catalog : row));
  const container = await mount(
    <MemoryRouter>
      <ActionRequestsProvider>
        <ActionRequestDetail requestId={row.id} />
        <ActionRequestDetail requestId={row.id} />
        <RefreshCatalog />
      </ActionRequestsProvider>
    </MemoryRouter>
  );
  await vi.waitFor(() => expect(sent.filter(({ path }) => path.endsWith("/action-groups"))).toHaveLength(1));
  await act(async () =>
    initialGroups.resolve([create(ActionGroupSchema, { key: "ssh", executorKind: ExecutorKind.POD })])
  );
  await vi.waitFor(() =>
    expect(
      [...container.querySelectorAll('[data-testid="action-execution-state"]')].map((node) => node.textContent)
    ).toEqual(["succeeded", "succeeded"])
  );

  catalog = [create(ActionGroupSchema, { key: "ssh", executorKind: ExecutorKind.MCP })];
  const refresh = [...container.querySelectorAll("button")].find((node) => node.textContent === "Refresh catalog");
  if (refresh === undefined) throw new Error("missing catalog refresh control");
  await act(async () => refresh.click());
  await vi.waitFor(() =>
    expect(
      [...container.querySelectorAll('[data-testid="action-execution-state"]')].map((node) => node.textContent)
    ).toEqual(["Tool error", "Tool error"])
  );
  expect(sent.filter(({ path }) => path.endsWith("/action-groups"))).toHaveLength(2);
});

async function finishOlderReadAfterWatch(newer: ActionRequest): Promise<{ container: HTMLDivElement; sent: Sent[] }> {
  const response = deferred<ActionRequest>();
  const sent = answerApi(({ method, path }) => {
    if (path.endsWith("/action-groups")) return [];
    if (method === "POST") return Response.json({ detail: "decision was rejected" }, { status: 409 });
    return response.promise;
  });
  const container = await mount(<App initialPath={`/actions/${ROW.id}`} />);
  await act(async () => {
    TestNdjsonWatch.current!.sendUpsertFrames([actionRequestUpsertProtoJson(newer)]);
  });
  await act(async () => response.resolve(ROW));
  return { container, sent };
}

it("uses a newer watch version for a decision after an older detail read finishes", async () => {
  const { container, sent } = await finishOlderReadAfterWatch({ ...ROW, version: 9_007_199_254_740_993n });
  expect(fetched(sent)).toBe(true);
  const approve = await vi.waitFor(() => {
    const button = container.querySelector<HTMLButtonElement>('button[aria-label="Approve"]');
    if (!button) throw new Error("missing Approve button");
    return button;
  });
  await act(async () => approve.click());
  await vi.waitFor(() => expect(container.textContent).toContain("decision was rejected"));
  const decisions = sent.filter(({ method }) => method === "POST");
  expect(decisions).toHaveLength(1);
  expect(decisions[0]?.body).toMatchObject({ expectedVersion: "9007199254740993" });
});

it("keeps a newer terminal watch receipt when an older pending detail read finishes", async () => {
  const { container, sent } = await finishOlderReadAfterWatch({
    ...decided(ActionState.ALLOWED),
    version: 9_007_199_254_740_993n,
  });
  expect(fetched(sent)).toBe(true);
  await vi.waitFor(() => expect(container.textContent).toContain("Allowed"));
  expect(container.querySelector('button[aria-label="Approve"]')).toBeNull();
});

it("shows a failed detail read without retrying and accepts a later watch update", async () => {
  const sent = answerApi(() => Response.json({ detail: "detail unavailable" }, { status: 503 }));
  const container = await mount(<App initialPath={`/actions/${ROW.id}`} />);
  await vi.waitFor(() => expect(container.textContent).toContain("detail unavailable"));
  await send([ROW]);
  await vi.waitFor(() => expect(container.querySelector('button[aria-label="Approve"]')).not.toBeNull());
  expect(container.textContent).not.toContain("detail unavailable");
  expect(sent).toHaveLength(1);
});

afterEach(async () => {
  await unmountLast();
  sessionStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
