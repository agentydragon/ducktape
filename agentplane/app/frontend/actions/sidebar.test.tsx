// @vitest-environment happy-dom

import { act, type JSX } from "react";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import { ActionsSidebarSection } from "./sidebar";
import { ActionRequestsProvider } from "./requests";
import { mount, request, unmountLast } from "./testing";

const stream: { current?: EventTarget } = {};

class ActionStream extends EventTarget {
  close = vi.fn();

  constructor(url: string) {
    super();
    if (url !== "/actions/stream?state=decision_pending") throw new Error(`unexpected Action stream URL: ${url}`);
    stream.current = this;
  }
}

function CurrentPath(): JSX.Element {
  const location = useLocation();
  return <div data-testid="current-path">{location.pathname}</div>;
}

async function send(rows: ReturnType<typeof request>[]): Promise<void> {
  await act(async () => {
    stream.current?.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(rows) }));
  });
}

afterEach(async () => {
  await unmountLast();
  sessionStorage.clear();
  localStorage.removeItem("agentplane-actions-sidebar-height");
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("resizes the open Actions area with the keyboard and remembers its height", async () => {
  vi.stubGlobal("EventSource", ActionStream);
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
  vi.stubGlobal("EventSource", ActionStream);
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

it("expands a compact action preview and only offers inline approval to eligible actions", async () => {
  vi.stubGlobal("EventSource", ActionStream);
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
  expect(container.textContent).toContain("Filters");
  expect(container.textContent).toContain("$ systemctl restart backup");
  const podDisclosure = container.querySelector<HTMLButtonElement>(`button[aria-label="Expand ${pod.title}"]`);
  if (!podDisclosure) throw new Error("missing pod action disclosure");
  await act(async () => podDisclosure.click());
  expect(container.textContent?.match(/List pods in namespace/g)).toHaveLength(1);
  expect(container.textContent).toContain("test-namespace");
  expect(container.querySelector(".agentplane-actions-sidebar-preview")?.textContent).not.toContain(
    "List pods in namespace"
  );
  expect(container.textContent).not.toContain("Requested by");
  expect(container.textContent).not.toContain("test description adding what the decision_pending title leaves out");
  const approveButton = container.querySelector<HTMLButtonElement>(`button[aria-label="Approve ${pod.title}"]`);
  const denyButton = container.querySelector<HTMLButtonElement>(`button[aria-label="Deny ${pod.title}"]`);
  expect(approveButton).not.toBeNull();
  expect(denyButton).not.toBeNull();
  expect(approveButton?.querySelector("svg")).not.toBeNull();
  expect(denyButton?.querySelector("svg")).not.toBeNull();
  expect(container.textContent).not.toContain("Exact arguments (unredacted)");

  const sshDisclosure = container.querySelector<HTMLButtonElement>(`button[aria-label="Expand ${ssh.title}"]`);
  if (!sshDisclosure) throw new Error("missing SSH action disclosure");
  await act(async () => sshDisclosure.click());
  expect(container.textContent).toContain("test-user@test-host.example");
  expect(container.textContent).toContain("systemctl restart backup");
  expect(container.querySelector<HTMLButtonElement>(`button[aria-label="Approve ${ssh.title}"]`)).toBeNull();
  expect(container.querySelector(`a[aria-label="View details for ${ssh.title}"]`)).not.toBeNull();
  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe("/threads/test-thread");
  const podDetails = container.querySelector<HTMLAnchorElement>(`a[aria-label="View details for ${pod.title}"]`);
  if (!podDetails) throw new Error("missing separate action-details arrow");
  await act(async () => podDetails.click());
  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe(`/actions/${pod.id}`);
});

it("shows approval widgets for Kubernetes resource, pod-list, and log reads", async () => {
  vi.stubGlobal("EventSource", ActionStream);
  const container = await mount(
    <MemoryRouter initialEntries={["/threads/test-thread"]}>
      <ActionRequestsProvider>
        <ActionsSidebarSection />
      </ActionRequestsProvider>
    </MemoryRouter>
  );
  const reads = [
    {
      ...request("decision_pending", 3),
      action: { group: "kubernetes_admin", name: "resources_get" },
      arguments: { apiVersion: "apps/v1", kind: "Deployment", name: "api" },
      title: "inspect deployment",
      visible: ["Get resource", "apps/v1", "Deployment", "namespace: (not specified)"],
    },
    {
      ...request("decision_pending", 4),
      action: { group: "kubernetes_admin", name: "pods_list_in_namespace" },
      arguments: { namespace: "test-namespace", labelSelector: "app=example", fieldSelector: "status.phase=Running" },
      title: "list namespace pods",
      visible: ["List pods in namespace", "test-namespace", "app=example", "status.phase=Running"],
    },
    {
      ...request("decision_pending", 5),
      action: { group: "kubernetes_admin", name: "pods_log" },
      arguments: { name: "api-0", container: "sidecar", previous: true, tail: -1 },
      title: "inspect pod logs",
      visible: ["Get pod logs", "api-0", "namespace: (not specified)", "sidecar", "previous: yes", "tail: -1"],
    },
  ];
  await send(reads);

  for (const read of reads) {
    const disclosure = container.querySelector<HTMLButtonElement>(`button[aria-label="Expand ${read.title}"]`);
    if (!disclosure) throw new Error(`missing disclosure for ${read.title}`);
    await act(async () => disclosure.click());
    for (const value of read.visible) expect(container.textContent).toContain(value);
    expect(container.querySelector<HTMLButtonElement>(`button[aria-label="Approve ${read.title}"]`)).not.toBeNull();
    expect(container.textContent).not.toContain("Decisions require full review.");
  }
});
