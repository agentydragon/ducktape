// @vitest-environment happy-dom

import { act, type JSX, type ReactNode } from "react";
import { Link, MemoryRouter, useLocation } from "react-router";
import { afterEach, expect, it, vi } from "vitest";
import { create } from "@bufbuild/protobuf";

import {
  ActionIdentitySchema,
  ActionState,
  type ActionRequest,
} from "../../../action_service/api/contracts/action_service_pb";
import { ActionsAttentionBar, ActionsSidebarSection } from "./sidebar";
import { ActionRequestsProvider } from "./requests";
import { answerApi, mount, request, send, unmountLast } from "./testing/harness";

function CurrentPath(): JSX.Element {
  const location = useLocation();
  return <div data-testid="current-path">{location.pathname}</div>;
}

function ActionsTestApp({ children }: { children: ReactNode }): JSX.Element {
  return (
    <MemoryRouter initialEntries={["/threads/test-thread"]}>
      <ActionRequestsProvider>{children}</ActionRequestsProvider>
    </MemoryRouter>
  );
}

/** The shell's placement: hidden, not unmounted, on the Actions pages. */
function StripOutsideActionPages(): JSX.Element {
  const location = useLocation();
  return (
    <>
      <ActionsAttentionBar visible={!location.pathname.startsWith("/actions")} />
      <Link to="/threads/test-thread">test return link</Link>
    </>
  );
}

afterEach(async () => {
  await unmountLast();
  sessionStorage.clear();
  localStorage.removeItem("agentplane-actions-sidebar-height");
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("resizes the open Actions area with the keyboard and remembers its height", async () => {
  const container = await mount(
    <ActionsTestApp>
      <CurrentPath />
      <ActionsSidebarSection />
    </ActionsTestApp>
  );
  await send([request(ActionState.DECISION_PENDING, 1)]);

  const separator = container.querySelector<HTMLElement>('[role="separator"][aria-label="Resize Actions area"]');
  if (!separator) throw new Error("missing Actions area resize separator");
  const originalHeight = Number(separator.getAttribute("aria-valuenow"));
  await act(async () => {
    separator.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }));
  });

  expect(Number(separator.getAttribute("aria-valuenow"))).toBe(originalHeight + 16);
  expect(localStorage.getItem("agentplane-actions-sidebar-height")).toBe(String(originalHeight + 16));
});

it("keeps the Actions section available and suppresses auto-open until the pending queue clears", async () => {
  const container = await mount(
    <ActionsTestApp>
      <CurrentPath />
      <ActionsSidebarSection />
    </ActionsTestApp>
  );
  const first = request(ActionState.DECISION_PENDING, 1);
  const second = request(ActionState.DECISION_PENDING, 2);
  const toggle = container.querySelector<HTMLButtonElement>(".agentplane-actions-sidebar-toggle");
  if (!toggle) throw new Error("missing always-visible Actions section");

  await send([]);
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  await send([first]);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  expect(container.textContent).toContain("1");

  await act(async () => toggle.click());
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  await send([first, second]);
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(container.textContent).toContain("2");
  expect(container.textContent).not.toContain(second.title);
  await act(async () => toggle.click());
  expect(container.textContent).toContain(second.title);

  await send([]);
  await send([second]);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe("/threads/test-thread");
});

it("expands a compact action preview and only offers a decision on the compact card to eligible actions", async () => {
  const container = await mount(
    <ActionsTestApp>
      <CurrentPath />
      <ActionsSidebarSection />
    </ActionsTestApp>
  );
  const pod = {
    ...request(ActionState.DECISION_PENDING, 1),
    action: create(ActionIdentitySchema, { group: "kubernetes_admin", name: "pods_list_in_namespace" }),
    arguments: { namespace: "test-namespace", labelSelector: "app=example" },
    title: "List pods in namespace test-namespace",
  };
  const ssh = {
    ...request(ActionState.DECISION_PENDING, 2),
    action: create(ActionIdentitySchema, { group: "ssh", name: "exec" }),
    arguments: { host: "test-host.example", user: "test-user", command: "systemctl restart backup" },
  };

  await send([pod, ssh]);
  const podLabel = [...container.querySelectorAll(".agentplane-actions-sidebar-name")].find((label) =>
    label.textContent?.includes("List pods in namespace test-namespace")
  );
  expect(podLabel).toBeDefined();
  expect(podLabel?.textContent).not.toContain("kubernetes_admin / pods_list_in_namespace");
  expect(container.textContent?.match(/List pods in namespace test-namespace/g)).toHaveLength(1);
  expect(container.textContent).toContain("Matching pods where");
  expect(container.textContent).toContain("systemctl restart backup");
  expect(container.textContent).not.toContain("Requested by");
  expect(container.textContent).not.toContain("test description adding what the decision_pending title leaves out");
  const approveButton = container.querySelector<HTMLButtonElement>(`button[aria-label="Approve ${pod.title}"]`);
  const denyButton = container.querySelector<HTMLButtonElement>(`button[aria-label="Deny ${pod.title}"]`);
  expect(approveButton).not.toBeNull();
  expect(denyButton).not.toBeNull();
  expect(approveButton?.querySelector("svg")).not.toBeNull();
  expect(denyButton?.querySelector("svg")).not.toBeNull();
  expect(container.textContent).not.toContain("Exact arguments");

  const sshDisclosure = container.querySelector<HTMLButtonElement>(`button[aria-label="Expand ${ssh.title}"]`);
  if (!sshDisclosure) throw new Error("missing SSH action disclosure");
  await act(async () => sshDisclosure.click());
  expect(container.textContent).toContain("test-user@test-host.example");
  expect(container.querySelector(".agentplane-actions-sidebar-preview")?.textContent).toContain(
    "systemctl restart backup"
  );
  expect(container.querySelector<HTMLButtonElement>(`button[aria-label="Approve ${ssh.title}"]`)).toBeNull();
  expect(container.querySelector(`a[aria-label="View details for ${ssh.title}"]`)).not.toBeNull();
  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe("/threads/test-thread");
  const podDetails = container.querySelector<HTMLAnchorElement>(`a[aria-label="View details for ${pod.title}"]`);
  if (!podDetails) throw new Error("missing separate action-details arrow");
  await act(async () => podDetails.click());
  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe(`/actions/${pod.id}`);
});

it("shows approval widgets for Kubernetes resource, pod-list, and log reads", async () => {
  const container = await mount(
    <ActionsTestApp>
      <ActionsSidebarSection />
    </ActionsTestApp>
  );
  const reads: { request: ActionRequest; visible: string[]; expands: boolean }[] = [
    {
      request: {
        ...request(ActionState.DECISION_PENDING, 3),
        action: create(ActionIdentitySchema, { group: "kubernetes_admin", name: "resources_get" }),
        arguments: { apiVersion: "apps/v1", kind: "Deployment", name: "api" },
        title: "inspect deployment",
      },
      visible: ["Read with API version apps/v1"],
      expands: true,
    },
    {
      request: {
        ...request(ActionState.DECISION_PENDING, 4),
        action: create(ActionIdentitySchema, { group: "kubernetes_admin", name: "pods_list_in_namespace" }),
        arguments: {
          namespace: "test-namespace",
          labelSelector: "app=example",
          fieldSelector: "status.phase=Running",
        },
        title: "list namespace pods",
      },
      visible: ["List pods in namespace", "test-namespace", "app=example", "status.phase=Running"],
      expands: false,
    },
    {
      request: {
        ...request(ActionState.DECISION_PENDING, 5),
        action: create(ActionIdentitySchema, { group: "kubernetes_admin", name: "pods_log" }),
        arguments: { name: "api-0", container: "sidecar", previous: true, tail: -1 },
        title: "inspect pod logs",
      },
      visible: ["container sidecar", "previous run", "last 100 lines (default)"],
      expands: false,
    },
    {
      request: {
        ...request(ActionState.DECISION_PENDING, 7),
        action: create(ActionIdentitySchema, { group: "gmail", name: "threads_list" }),
        arguments: { q: "from:deployments@example.com", maxResults: 5 },
        title: "search deployment mail",
      },
      visible: ["from:deployments@example.com", "Up to 5 threads"],
      expands: false,
    },
  ];
  await send(reads.map(({ request: row }) => row));
  await vi.waitFor(() => expect(container.textContent).toContain("previous run"));
  for (const { request: read, visible, expands } of reads) {
    const disclosure = container.querySelector<HTMLButtonElement>(`button[aria-label="Expand ${read.title}"]`);
    expect(disclosure !== null).toBe(expands);
    if (disclosure) await act(async () => disclosure.click());
    for (const value of visible) expect(container.textContent).toContain(value);
    expect(container.querySelector<HTMLButtonElement>(`button[aria-label="Approve ${read.title}"]`)).not.toBeNull();
    expect(container.textContent).not.toContain("Decisions require full review.");
  }
});

it("leaves the disclosure off a card whose opened version would add nothing", async () => {
  const container = await mount(
    <ActionsTestApp>
      <CurrentPath />
      <ActionsSidebarSection />
    </ActionsTestApp>
  );
  const read = {
    ...request(ActionState.DECISION_PENDING, 8),
    action: create(ActionIdentitySchema, { group: "sandbox", name: "get" }),
    arguments: { name: "test-sandbox" },
    title: "inspect the test sandbox",
  };
  const unparsed = {
    ...request(ActionState.DECISION_PENDING, 9),
    action: read.action,
    arguments: { name: "test-sandbox", test_extra: "shown on opening" },
    title: "inspect the test sandbox with an unknown argument",
  };
  await send([read, unparsed]);

  expect(container.querySelector(`button[aria-label="Expand ${read.title}"]`)).toBeNull();
  expect(container.textContent).toContain("Inspect sandbox test-sandbox");
  expect(container.textContent).toContain(read.title);
  expect(container.querySelector(`button[aria-label="Approve ${read.title}"]`)).not.toBeNull();
  expect(container.querySelector(`button[aria-label="Deny ${read.title}"]`)).not.toBeNull();
  const details = container.querySelector<HTMLAnchorElement>(`a[aria-label="View details for ${read.title}"]`);
  if (!details) throw new Error("missing details arrow on a card without an expanded version");

  // Arguments the Action cannot draw keep the expanded version, which shows them as recorded.
  const disclosure = container.querySelector<HTMLButtonElement>(`button[aria-label="Expand ${unparsed.title}"]`);
  if (!disclosure) throw new Error("missing disclosure for undrawn arguments");
  await act(async () => disclosure.click());
  expect(container.querySelector(".agentplane-actions-sidebar-preview")?.textContent).toContain("shown on opening");

  await act(async () => details.click());
  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe(`/actions/${read.id}`);
});

it("offers Approve and Deny on a collapsed card of an opted-in Action, including one from an external caller", async () => {
  const container = await mount(
    <ActionsTestApp>
      <ActionsSidebarSection />
    </ActionsTestApp>
  );
  const read = {
    ...request(ActionState.DECISION_PENDING, 6),
    action: create(ActionIdentitySchema, { group: "kubernetes_admin", name: "resources_get" }),
    arguments: { apiVersion: "v1", kind: "Pod", name: "api-0" },
    title: "inspect pod",
  };
  await send([read]);
  expect(container.querySelector(`button[aria-expanded="true"][aria-label^="Collapse"]`)).toBeNull();
  expect(container.querySelector(`button[aria-label="Approve ${read.title}"]`)).not.toBeNull();
  expect(container.querySelector(`button[aria-label="Deny ${read.title}"]`)).not.toBeNull();
});

it("shows compact decisions only for a fully displayed mutation", async () => {
  const container = await mount(
    <ActionsTestApp>
      <ActionsSidebarSection />
    </ActionsTestApp>
  );
  const calendar = create(ActionIdentitySchema, { group: "google_calendar", name: "create_event" });
  const simple = { summary: "Office closure", start: { date: "2026-11-26" }, end: { date: "2026-11-27" } };
  const makeRequest = (i: number, args: ActionRequest["arguments"]) => ({
    ...request(ActionState.DECISION_PENDING, i),
    action: calendar,
    arguments: args,
  });
  const requests = [
    makeRequest(1, simple),
    makeRequest(2, { ...simple, calendar_id: "shared@example.test" }),
    makeRequest(3, { ...simple, description: "Agenda to review" }),
    makeRequest(4, { ...simple, send_updates: "all" }),
  ];
  await send(requests);

  const cards = container.querySelectorAll(".agentplane-actions-sidebar-request");
  expect(cards[0]?.textContent).toContain("Office closure");
  expect(cards[0]?.textContent).toContain("Primary calendar");
  expect(cards[0]?.textContent).toContain("Use calendar defaults");
  expect(cards[0]?.querySelector('button[aria-label^="Approve "]')).not.toBeNull();
  expect(cards[0]?.querySelector('button[aria-label^="Deny "]')).not.toBeNull();
  expect(cards[0]?.querySelector("button[aria-expanded]")).toBeNull();

  for (const card of [...cards].slice(1)) {
    expect(card.querySelector('button[aria-label^="Approve "]')).toBeNull();
    const disclosure = card.querySelector<HTMLButtonElement>('button[aria-expanded="false"]');
    if (!disclosure) throw new Error("missing disclosure for a request requiring review");
    await act(async () => disclosure.click());
    expect(card.querySelector('button[aria-label^="Approve "]')).toBeNull();
  }
  expect(cards[3]?.textContent).toContain("send_updates");
});

it("shows draft decisions only after opening the complete new draft", async () => {
  const container = await mount(
    <ActionsTestApp>
      <ActionsSidebarSection />
    </ActionsTestApp>
  );
  await send([
    {
      ...request(ActionState.DECISION_PENDING, 1),
      action: create(ActionIdentitySchema, { group: "gmail", name: "drafts_create" }),
      arguments: {
        to: ["reviewer@example.test"],
        bcc: ["archive@example.test"],
        subject: "Release notes",
        body: "First line\nSecond line\nThird line\nLast line",
      },
    },
  ]);
  const card = container.querySelector(".agentplane-actions-sidebar-request");
  expect(card?.querySelector('button[aria-label^="Approve "]')).toBeNull();
  const disclosure = card?.querySelector<HTMLButtonElement>('button[aria-expanded="false"]');
  if (!disclosure) throw new Error("missing draft disclosure");
  await act(async () => disclosure.click());
  const preview = card?.querySelector(".agentplane-actions-sidebar-preview");
  expect(preview?.textContent).toContain("Last line");
  expect(preview?.textContent).toContain("archive@example.test");
  expect(preview?.querySelector('button[aria-label^="Approve "]')).not.toBeNull();
});

const expandedDecisionCases: {
  group: string;
  name: string;
  endpoint: string;
  args: NonNullable<ActionRequest["arguments"]>;
  bulk: NonNullable<ActionRequest["arguments"]>;
  hidden: NonNullable<ActionRequest["arguments"]>;
  response: unknown;
  partial: unknown;
  completeText: string[];
  partialText: string;
}[] = [
  {
    group: "gmail",
    name: "threads_modify_labels",
    endpoint: "gmail-thread-summaries",
    args: { thread_ids: ["thread-123"], add: ["Reviewed"], remove: ["Needs review"] },
    bulk: { thread_ids: ["thread-123", "thread-456"], add: ["Reviewed"] },
    hidden: { thread_ids: ["thread-123"], add: ["Needs review, Reviewed"] },
    response: {
      threads: [
        { threadId: "thread-123", subject: "Deployment review" },
        { threadId: "thread-456", subject: "Quarterly review" },
      ],
    },
    partial: { threads: [{ threadId: "thread-456", subject: "Unrelated thread" }] },
    completeText: ["Deployment review", "Add Reviewed", "Remove Needs review"],
    partialText: "thread-123",
  },
  {
    group: "grocy_sf",
    name: "products_edit",
    endpoint: "grocy-reference-summaries",
    args: { items: [{ product: 2, name: "Rolled oats", location: 5 }] },
    bulk: {
      items: [
        { product: 2, location: 5 },
        { product: 3, name: "Rice" },
      ],
    },
    hidden: { items: [{ product: 2, stock_qu: 6 }] },
    response: {
      references: [
        { key: "product_edits/2", name: "Oats", productEditCurrent: { locationId: "4", stockUnitId: "7" } },
        { key: "product_edits/3", name: "Brown rice", productEditCurrent: {} },
        { key: "locations/4", name: "Cupboard" },
        { key: "locations/5", name: "Pantry" },
        { key: "quantity_units/6", name: "Kilogram" },
        { key: "quantity_units/7", name: "Bag" },
      ],
    },
    partial: {
      references: [
        { key: "product_edits/2", name: "Oats", productEditCurrent: { locationId: "4" } },
        { key: "locations/4", name: "Cupboard" },
      ],
    },
    completeText: ["Oats → Rolled oats", "Cupboard → Pantry"],
    partialText: "Cupboard → Location #5",
  },
];

it.each(expandedDecisionCases)("keeps $group decisions behind the fully resolved expanded preview", async (example) => {
  const container = await mount(
    <ActionsTestApp>
      <ActionsSidebarSection />
    </ActionsTestApp>
  );
  const { args, bulk, hidden, response, partial } = example;
  const action = create(ActionIdentitySchema, { group: example.group, name: example.name });
  const fixtures = [args, args, args, bulk, hidden, { ...args, unknown: true }].map((argumentsValue, index) => ({
    ...request(ActionState.DECISION_PENDING, index + 1),
    action,
    arguments: argumentsValue,
  }));
  let completeLookup!: (value: unknown) => void;
  const pendingLookup = new Promise<unknown>((resolve) => {
    completeLookup = resolve;
  });
  const sent = answerApi(({ path }) => {
    if (!path.endsWith(example.endpoint)) return undefined;
    const fixtureIndex = fixtures.findIndex(({ id }) => path.includes(id));
    return [pendingLookup, Response.json({}, { status: 503 }), partial, response, response][fixtureIndex];
  });
  await send(fixtures);

  const cards = [...container.querySelectorAll(".agentplane-actions-sidebar-request")];
  expect(cards).toHaveLength(fixtures.length);
  function decision(card: Element) {
    return card.querySelector('button[aria-label^="Approve "]');
  }
  async function open(card: Element) {
    const toggle = card.querySelector<HTMLButtonElement>('button[aria-expanded="false"]');
    if (!toggle) throw new Error("missing action disclosure");
    await act(async () => toggle.click());
  }
  const [matching, failed, incomplete, multiple, undisplayed, unknown] = cards as [
    Element,
    Element,
    Element,
    Element,
    Element,
    Element,
  ];
  expect(decision(matching)).toBeNull();
  await open(matching);
  expect(decision(matching)).toBeNull();
  await act(async () => completeLookup(response));
  await vi.waitFor(() => expect(decision(matching)).not.toBeNull());
  for (const text of example.completeText) expect(matching.textContent).toContain(text);

  for (const card of [failed, incomplete, multiple, undisplayed, unknown]) {
    await open(card);
    expect(decision(card)).toBeNull();
  }
  await vi.waitFor(() => {
    const previewed = sent
      .filter(({ path }) => path.endsWith(example.endpoint))
      .map(({ path }) => path.split("/").at(-2));
    expect(new Set(previewed)).toEqual(new Set(fixtures.slice(0, 5).map(({ id }) => id)));
  });
  await open(incomplete);
  await vi.waitFor(() => expect(incomplete.textContent).toContain(example.partialText));
  expect(decision(incomplete)).toBeNull();
});

it("shows the first two pending requests in the closed-sidebar strip and the rest behind its expansion", async () => {
  const container = await mount(
    <ActionsTestApp>
      <ActionsAttentionBar visible />
    </ActionsTestApp>
  );
  const [first, second, third] = [1, 2, 3].map((index) => request(ActionState.DECISION_PENDING, index)) as [
    ActionRequest,
    ActionRequest,
    ActionRequest,
  ];
  const shown = (): (string | undefined)[] =>
    Array.from(container.querySelectorAll<HTMLAnchorElement>(".agentplane-actions-sidebar-request-heading a"), (link) =>
      link.getAttribute("href")?.split("/").at(-1)
    );
  const expansion = (): HTMLButtonElement | null =>
    container.querySelector<HTMLButtonElement>('button[aria-controls="agentplane-actions-attention-content"]');

  await send([]);
  expect(container.querySelector(".agentplane-actions-attention")).toBeNull();
  await send([first, second]);
  expect(shown()).toEqual([first.id, second.id]);
  expect(expansion()).toBeNull();

  await send([first, second, third]);
  expect(shown()).toEqual([first.id, second.id]);
  const more = expansion();
  if (!more) throw new Error("missing expansion for the hidden pending request");
  expect(more.getAttribute("aria-expanded")).toBe("false");
  await act(async () => more.click());
  expect(more.getAttribute("aria-expanded")).toBe("true");
  expect(shown()).toEqual([first.id, second.id, third.id]);

  // Once the queue fits again, a later overflow starts collapsed.
  await send([first]);
  expect(expansion()).toBeNull();
  await send([first, second, third]);
  expect(shown()).toEqual([first.id, second.id]);
  await send([]);
  expect(container.querySelector(".agentplane-actions-attention")).toBeNull();
});

it("keeps the strip's expansion across a visit to a details page that decides one request", async () => {
  const container = await mount(
    <ActionsTestApp>
      <StripOutsideActionPages />
    </ActionsTestApp>
  );
  const pending = [1, 2, 3, 4].map((index) => request(ActionState.DECISION_PENDING, index));
  const decided = pending[2]!;
  await send(pending);
  const expansion = (): HTMLButtonElement | null =>
    container.querySelector<HTMLButtonElement>('button[aria-controls="agentplane-actions-attention-content"]');
  await act(async () => expansion()?.click());
  expect(expansion()?.getAttribute("aria-expanded")).toBe("true");

  const details = container.querySelector<HTMLAnchorElement>(`a[href="/actions/${decided.id}"]`);
  if (!details) throw new Error("missing details link for the request to decide");
  await act(async () => details.click());
  expect(container.querySelector(".agentplane-actions-attention")).toBeNull();
  await send(pending.filter((request) => request.id !== decided.id));
  const back = container.querySelector<HTMLAnchorElement>('a[href="/threads/test-thread"]');
  if (!back) throw new Error("missing return link");
  await act(async () => back.click());

  expect(expansion()?.getAttribute("aria-expanded")).toBe("true");
  expect(container.querySelectorAll(".agentplane-actions-sidebar-request")).toHaveLength(3);
});
