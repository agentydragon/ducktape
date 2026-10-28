// @vitest-environment happy-dom

import { act, type JSX } from "react";
import { Link, MemoryRouter, useLocation } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import { ActionsAttentionBar, ActionsSidebarSection } from "./sidebar";
import { ActionRequestsProvider } from "./requests";
import { mount, request, send, unmountLast } from "./testing/harness";
import type { ActionRequestView } from "./types";

function CurrentPath(): JSX.Element {
  const location = useLocation();
  return <div data-testid="current-path">{location.pathname}</div>;
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
    <MemoryRouter initialEntries={["/threads/test-thread"]}>
      <ActionRequestsProvider>
        <CurrentPath />
        <ActionsSidebarSection />
      </ActionRequestsProvider>
    </MemoryRouter>
  );
  await send([request("decision_pending", 1)]);

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
    <MemoryRouter initialEntries={["/threads/test-thread"]}>
      <ActionRequestsProvider>
        <CurrentPath />
        <ActionsSidebarSection />
      </ActionRequestsProvider>
    </MemoryRouter>
  );
  const first = request("decision_pending", 1);
  const second = request("decision_pending", 2);
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
    <MemoryRouter initialEntries={["/threads/test-thread"]}>
      <ActionRequestsProvider>
        <CurrentPath />
        <ActionsSidebarSection />
      </ActionRequestsProvider>
    </MemoryRouter>
  );
  const pod = {
    ...request("decision_pending", 1),
    action: { group: "kubernetes_admin", name: "pods_list_in_namespace" },
    arguments: { namespace: "test-namespace", labelSelector: "app=example" },
    title: "List pods in namespace test-namespace",
  };
  const ssh = {
    ...request("decision_pending", 2),
    action: { group: "ssh", name: "exec" },
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
    <MemoryRouter initialEntries={["/threads/test-thread"]}>
      <ActionRequestsProvider>
        <ActionsSidebarSection />
      </ActionRequestsProvider>
    </MemoryRouter>
  );
  const reads: { request: ActionRequestView; visible: string[]; expands: boolean }[] = [
    {
      request: {
        ...request("decision_pending", 3),
        action: { group: "kubernetes_admin", name: "resources_get" },
        arguments: { apiVersion: "apps/v1", kind: "Deployment", name: "api" },
        title: "inspect deployment",
      },
      visible: ["Read with API version apps/v1"],
      expands: true,
    },
    {
      request: {
        ...request("decision_pending", 4),
        action: { group: "kubernetes_admin", name: "pods_list_in_namespace" },
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
        ...request("decision_pending", 5),
        action: { group: "kubernetes_admin", name: "pods_log" },
        arguments: { name: "api-0", container: "sidecar", previous: true, tail: -1 },
        title: "inspect pod logs",
      },
      visible: ["container sidecar", "previous run", "tail -1"],
      expands: false,
    },
    {
      request: {
        ...request("decision_pending", 7),
        action: { group: "gmail", name: "threads_list" },
        arguments: { q: "from:deployments@example.com", maxResults: 5 },
        title: "search deployment mail",
      },
      visible: ["from:deployments@example.com", '"maxResults": 5'],
      expands: true,
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
    <MemoryRouter initialEntries={["/threads/test-thread"]}>
      <ActionRequestsProvider>
        <CurrentPath />
        <ActionsSidebarSection />
      </ActionRequestsProvider>
    </MemoryRouter>
  );
  const read = {
    ...request("decision_pending", 8),
    action: { group: "sandbox", name: "get" },
    arguments: { name: "test-sandbox" },
    title: "inspect the test sandbox",
  };
  const unparsed = {
    ...request("decision_pending", 9),
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
    <MemoryRouter initialEntries={["/threads/test-thread"]}>
      <ActionRequestsProvider>
        <ActionsSidebarSection />
      </ActionRequestsProvider>
    </MemoryRouter>
  );
  const read = {
    ...request("decision_pending", 6),
    action: { group: "kubernetes_admin", name: "resources_get" },
    arguments: { apiVersion: "v1", kind: "Pod", name: "api-0" },
    title: "inspect pod",
  };
  await send([read]);
  expect(container.querySelector(`button[aria-expanded="true"][aria-label^="Collapse"]`)).toBeNull();
  expect(container.querySelector(`button[aria-label="Approve ${read.title}"]`)).not.toBeNull();
  expect(container.querySelector(`button[aria-label="Deny ${read.title}"]`)).not.toBeNull();
});

it("shows the first two pending requests in the closed-sidebar strip and the rest behind its expansion", async () => {
  const container = await mount(
    <MemoryRouter initialEntries={["/threads/test-thread"]}>
      <ActionRequestsProvider>
        <ActionsAttentionBar visible />
      </ActionRequestsProvider>
    </MemoryRouter>
  );
  const [first, second, third] = [1, 2, 3].map((index) => request("decision_pending", index)) as [
    ActionRequestView,
    ActionRequestView,
    ActionRequestView,
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
    <MemoryRouter initialEntries={["/threads/test-thread"]}>
      <ActionRequestsProvider>
        <StripOutsideActionPages />
      </ActionRequestsProvider>
    </MemoryRouter>
  );
  const pending = [1, 2, 3, 4].map((index) => request("decision_pending", index));
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
