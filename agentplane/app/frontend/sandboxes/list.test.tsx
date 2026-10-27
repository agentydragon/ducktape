// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { TEST_REASONING_EFFORTS, testModelCatalog } from "../test_model_catalog";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import { api } from "../client";
import { type NewSandboxDefaults, type SandboxPresetView, type SandboxView } from "./client";
import { SandboxList } from "./list";
import { deletable, SuspendResume } from "./lifecycle";
import { sandboxSummary } from "../sandbox_status";

const liveSandboxData = vi.hoisted(() => ({ snapshot: null as { sandboxes: unknown[] } | null }));

vi.mock("../live", () => ({
  liveSandboxesUrl: () => "/live/sandboxes",
  useLive: () => liveSandboxData,
}));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];
afterEach(async () => {
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
  liveSandboxData.snapshot = null;
  vi.restoreAllMocks();
});

const CREATED: SandboxView = {
  name: "test-created-sandbox",
  uid: "00000000-0000-4000-8000-000000000001",
  namespace: "agentplane-test",
  created_at: "2026-01-01T00:00:00Z",
  operating_mode: "Running",
  service_account: { namespace: "agentplane-test", name: "test-created-sandbox" },
  status: null,
  phase: "Creating",
  pod: null,
  wake_on_send: false,
};

// What the app serves when its configuration leaves the new-Sandbox defaults unset.
const UNSET_DEFAULTS: NewSandboxDefaults = { wake_on_send: false, idle_suspend_after_seconds: null };

async function render(newSandboxDefaults: NewSandboxDefaults = UNSET_DEFAULTS) {
  const onOpen = vi.fn();
  const preset: SandboxPresetView = {
    name: "test-preset",
    title: "Test preset",
    template: "test-template",
    policy_sets: ["test-basic"],
    inbox_tags: ["test-group"],
    session_defaults: { harness: "HARNESS_CODEX", model: "test-codex-b" },
    bootstrap: "mkdir -p /state/workspaces",
  };
  vi.spyOn(api, "GET").mockImplementation(async (path) => {
    const data =
      path === "/models"
        ? testModelCatalog(
            [{ model: "test-claude", display_name: "Test Claude", reasoning_efforts: TEST_REASONING_EFFORTS }],
            [
              { model: "test-codex-a", display_name: "Test Codex A", reasoning_efforts: TEST_REASONING_EFFORTS },
              { model: "test-codex-b", display_name: "Test Codex B", reasoning_efforts: TEST_REASONING_EFFORTS },
            ]
          )
        : path === "/presets"
          ? [preset]
          : path === "/sandboxes/form"
            ? { policy_sets: ["test-basic", "test-packages"], new_sandbox_defaults: newSandboxDefaults }
            : path === "/sandboxes/templates"
              ? ["test-template", "other-template"]
              : [];
    return { data, response: new Response() } as Awaited<ReturnType<typeof api.GET>>;
  });
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <MemoryRouter>
          <SandboxList onOpen={onOpen} />
        </MemoryRouter>
      </MantineProvider>
    )
  );
  await choose(container, "Preset", "Test preset");
  return { container, onOpen };
}

function input(container: HTMLElement, label: string): HTMLInputElement {
  const element = [...container.querySelectorAll("label")].find((node) => node.textContent === label);
  const control = element?.control;
  if (!(control instanceof HTMLInputElement)) throw new Error(`Missing ${label} input`);
  return control;
}

async function choose(container: HTMLElement, label: string, value: string): Promise<void> {
  await act(async () => input(container, label).click());
  const option = options(container, label).find((node) => node.textContent === value);
  if (!option) throw new Error(`Missing ${value} option`);
  await act(async () => option.click());
}

/** Type into a controlled input the way a keyboard does, so React sees the change. */
async function type(element: HTMLInputElement, value: string): Promise<void> {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  if (!setter) throw new Error("HTMLInputElement.value has no setter");
  await act(async () => {
    setter.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function options(container: HTMLElement, label: string): HTMLElement[] {
  const listId = input(container, label).getAttribute("aria-controls");
  const list = listId ? document.getElementById(listId) : null;
  if (!list) throw new Error(`Missing ${label} options`);
  return [...list.querySelectorAll<HTMLElement>('[role="option"]')];
}

it("keeps session defaults in the app config instead of writing them onto a Sandbox", async () => {
  const { container } = await render();
  expect(input(container, "Sandbox template").value).toBe("test-template");
  expect(container.textContent).toContain("session defaults stay in app configuration");
  expect(container.textContent).not.toContain("Session launch defaults");
});

it("pre-fills the preset's policy sets and inbox tags, hides picked sets, and sends the picks", async () => {
  const { container, onOpen } = await render();
  expect(container.textContent).toContain("test-basic");
  expect(container.textContent).toContain("test-group");
  await act(async () => input(container, "Policy sets").click());
  expect(options(container, "Policy sets").map((node) => node.textContent)).toEqual(["test-packages"]);
  await act(async () => input(container, "Policy sets").click());
  await choose(container, "Policy sets", "test-packages");
  const post = vi.spyOn(api, "POST").mockResolvedValue({ data: CREATED, response: new Response() } as never);
  await type(input(container, "Name"), "picked");
  const button = [...container.querySelectorAll("button")].find((node) => node.textContent === "New sandbox");
  if (!button) throw new Error("Missing New sandbox button");
  await act(async () => button.click());
  expect(post).toHaveBeenCalledWith("/sandboxes", {
    body: {
      name: "picked",
      template: "test-template",
      preset: "test-preset",
      policy_sets: ["test-basic", "test-packages"],
      inbox_tags: ["test-group"],
      bootstrap: "mkdir -p /state/workspaces",
      wake_on_send: false,
      idle_suspend_after_seconds: null,
    },
  });
  expect(onOpen).toHaveBeenCalledExactlyOnceWith(CREATED.name);
});

it("allows an operator to remove the preset's policy set and sends an explicit empty list", async () => {
  const { container } = await render();
  await act(async () => {
    const pill = [...container.querySelectorAll<HTMLElement>(".mantine-Pill-root")].find((node) =>
      node.textContent?.includes("test-basic")
    );
    const remove = pill?.querySelector<HTMLButtonElement>(".mantine-Pill-remove");
    if (!remove) throw new Error("Missing selected test-basic policy set control");
    remove.click();
  });
  const post = vi.spyOn(api, "POST").mockResolvedValue({ data: CREATED, response: new Response() } as never);
  await type(input(container, "Name"), "without-sets");
  const button = [...container.querySelectorAll("button")].find((node) => node.textContent === "New sandbox");
  if (!button) throw new Error("Missing New sandbox button");
  await act(async () => button.click());
  expect(post).toHaveBeenCalledWith(
    "/sandboxes",
    expect.objectContaining({ body: expect.objectContaining({ policy_sets: [] }) })
  );
});

it("lets an operator replace the preset template before creating the sandbox", async () => {
  const { container } = await render();
  await choose(container, "Sandbox template", "other-template");
  const post = vi.spyOn(api, "POST").mockResolvedValue({ data: CREATED, response: new Response() } as never);
  await type(input(container, "Name"), "picked");
  const button = [...container.querySelectorAll("button")].find((node) => node.textContent === "New sandbox");
  if (!button) throw new Error("Missing New sandbox button");
  await act(async () => button.click());

  expect(post).toHaveBeenCalledWith(
    "/sandboxes",
    expect.objectContaining({ body: expect.objectContaining({ template: "other-template" }) })
  );
});

it("keeps the creation form and reports rejection without navigating", async () => {
  const { container, onOpen } = await render();
  vi.spyOn(api, "POST").mockResolvedValue({
    error: { detail: "Test creation refused" },
    response: new Response(null, { status: 409 }),
  } as never);
  await type(input(container, "Name"), "test-not-created");
  const button = [...container.querySelectorAll("button")].find((node) => node.textContent === "New sandbox");
  if (!button) throw new Error("Missing New sandbox button");
  await act(async () => button.click());
  expect(onOpen).not.toHaveBeenCalled();
  expect(input(container, "Name").value).toBe("test-not-created");
  expect(container.textContent).toContain("Test creation refused");
});

it("lets a suspended sandbox be deleted", () => {
  const suspended: SandboxView = { ...CREATED, operating_mode: "Suspended", phase: "Suspended" };
  expect(sandboxSummary(suspended)).toEqual({ label: "Suspended", kind: "suspended" });
  expect(deletable(suspended)).toBe(true);
});

it("keeps lifecycle actions unavailable until the controller reports a mode", async () => {
  const unknown: SandboxView = { ...CREATED, operating_mode: null, phase: "Unspecified" };
  const onAct = vi.fn();
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <SuspendResume sandbox={unknown} onAct={onAct} />
      </MantineProvider>
    )
  );

  expect(container.querySelector<HTMLButtonElement>('[aria-label="Sandbox mode unavailable"]')?.disabled).toBe(true);
  expect(deletable(unknown)).toBe(false);
});

it("creates under the preset-derived placeholder when the name is left empty", async () => {
  const { container } = await render();
  expect(input(container, "Name").placeholder).toBe("test-preset-1");
  const post = vi.spyOn(api, "POST").mockResolvedValue({ data: CREATED, response: new Response() } as never);
  const button = [...container.querySelectorAll("button")].find((node) => node.textContent === "New sandbox");
  if (!button) throw new Error("Missing New sandbox button");
  await act(async () => button.click());
  expect(post).toHaveBeenCalledWith(
    "/sandboxes",
    expect.objectContaining({ body: expect.objectContaining({ name: "test-preset-1" }) })
  );
});

it("lets an operator opt a new Sandbox into wake on message", async () => {
  const { container } = await render();
  const wakeCheckbox = container.querySelector<HTMLInputElement>('input[type="checkbox"]');
  if (!wakeCheckbox) throw new Error("Missing Wake on message checkbox");
  expect(wakeCheckbox.checked).toBe(false);
  await act(async () => wakeCheckbox.click());
  expect(wakeCheckbox.checked).toBe(true);

  const post = vi.spyOn(api, "POST").mockResolvedValue({ data: CREATED, response: new Response() } as never);
  await type(input(container, "Name"), "wake-enabled");
  const button = [...container.querySelectorAll("button")].find((node) => node.textContent === "New sandbox");
  if (!button) throw new Error("Missing New sandbox button");
  await act(async () => button.click());

  expect(post).toHaveBeenCalledWith(
    "/sandboxes",
    expect.objectContaining({ body: expect.objectContaining({ wake_on_send: true }) })
  );
});

it("starts a new Sandbox with the app's wake and idle suspension defaults whatever the preset", async () => {
  const { container } = await render({ wake_on_send: true, idle_suspend_after_seconds: 900 });
  const wakeCheckbox = container.querySelector<HTMLInputElement>('input[type="checkbox"]');
  if (!wakeCheckbox) throw new Error("Missing Wake on message checkbox");
  expect(wakeCheckbox.checked).toBe(true);
  expect(input(container, "Suspend when idle (min)").value).toBe("15");

  const post = vi.spyOn(api, "POST").mockResolvedValue({ data: CREATED, response: new Response() } as never);
  await type(input(container, "Name"), "defaulted");
  const button = [...container.querySelectorAll("button")].find((node) => node.textContent === "New sandbox");
  if (!button) throw new Error("Missing New sandbox button");
  await act(async () => button.click());

  expect(post).toHaveBeenCalledWith(
    "/sandboxes",
    expect.objectContaining({
      body: expect.objectContaining({ wake_on_send: true, idle_suspend_after_seconds: 900 }),
    })
  );
});

it("shows existing wake policy and updates the displayed Sandbox incarnation", async () => {
  liveSandboxData.snapshot = { sandboxes: [{ ...CREATED, wake_on_send: true }] };
  const { container } = await render();
  const checkbox = container.querySelector<HTMLInputElement>(
    'input[aria-label="Wake on message for test-created-sandbox"]'
  );
  if (!checkbox) throw new Error("Missing existing Sandbox Wake on message checkbox");
  expect(checkbox.checked).toBe(true);

  const patch = vi.spyOn(api, "PATCH").mockResolvedValue({
    data: undefined,
    response: new Response(null, { status: 204 }),
  } as never);
  await act(async () => checkbox.click());

  expect(patch).toHaveBeenCalledWith("/sandboxes/{name}/wake-policy", {
    params: { path: { name: CREATED.name } },
    body: { uid: CREATED.uid, wake_on_send: false },
  });
  expect(checkbox.checked).toBe(false);
});

it("clears the pending wake policy when the request fails at the transport", async () => {
  liveSandboxData.snapshot = { sandboxes: [{ ...CREATED, wake_on_send: false }] };
  const { container } = await render();
  const checkbox = container.querySelector<HTMLInputElement>(
    'input[aria-label="Wake on message for test-created-sandbox"]'
  );
  if (!checkbox) throw new Error("Missing existing Sandbox Wake on message checkbox");
  vi.spyOn(api, "PATCH").mockRejectedValue(new Error("Network disconnected"));

  await act(async () => checkbox.click());

  expect(checkbox.checked).toBe(false);
  expect(checkbox.disabled).toBe(false);
  expect(container.textContent).toContain("Network disconnected");
});

it("flags an invalid typed name and refuses to create", async () => {
  const { container } = await render();
  await type(input(container, "Name"), "Bad_Name");
  expect(input(container, "Name").getAttribute("aria-invalid")).toBe("true");
  const button = [...container.querySelectorAll("button")].find((node) => node.textContent === "New sandbox");
  expect(button?.hasAttribute("disabled")).toBe(true);
});

it("sends the idle suspension time typed in minutes as seconds", async () => {
  const { container } = await render();
  await type(input(container, "Suspend when idle (min)"), "90");
  const post = vi.spyOn(api, "POST").mockResolvedValue({ data: CREATED, response: new Response() } as never);
  await type(input(container, "Name"), "napper");
  const button = [...container.querySelectorAll("button")].find((node) => node.textContent === "New sandbox");
  if (!button) throw new Error("Missing New sandbox button");
  await act(async () => button.click());

  expect(post).toHaveBeenCalledWith(
    "/sandboxes",
    expect.objectContaining({ body: expect.objectContaining({ idle_suspend_after_seconds: 5400 }) })
  );
});

function idleInput(container: HTMLElement): HTMLInputElement {
  const control = container.querySelector<HTMLInputElement>(
    'input[aria-label="Idle suspend minutes for test-created-sandbox"]'
  );
  if (!control) throw new Error("Missing existing Sandbox idle suspend input");
  return control;
}

async function press(element: HTMLElement, key: string): Promise<void> {
  await act(async () => {
    element.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
  });
}

it("shows a Sandbox's idle suspension time and saves an edit to the incarnation shown", async () => {
  liveSandboxData.snapshot = { sandboxes: [{ ...CREATED, idle_suspend_after_seconds: 1800 }] };
  const { container } = await render();
  expect(idleInput(container).value).toBe("30");

  const patch = vi.spyOn(api, "PATCH").mockResolvedValue({
    data: undefined,
    response: new Response(null, { status: 204 }),
  } as never);
  await type(idleInput(container), "45");
  await press(idleInput(container), "Enter");

  expect(patch).toHaveBeenCalledWith("/sandboxes/{name}/idle-suspend", {
    params: { path: { name: CREATED.name } },
    body: { uid: CREATED.uid, idle_suspend_after_seconds: 2700 },
  });
});

it("turns idle suspension off when the time is cleared", async () => {
  liveSandboxData.snapshot = { sandboxes: [{ ...CREATED, idle_suspend_after_seconds: 1800 }] };
  const { container } = await render();
  const patch = vi.spyOn(api, "PATCH").mockResolvedValue({
    data: undefined,
    response: new Response(null, { status: 204 }),
  } as never);

  await type(idleInput(container), "");
  await press(idleInput(container), "Enter");

  expect(patch).toHaveBeenCalledWith("/sandboxes/{name}/idle-suspend", {
    params: { path: { name: CREATED.name } },
    body: { uid: CREATED.uid, idle_suspend_after_seconds: null },
  });
});

it("puts back the stored idle suspension time and reports a refused edit", async () => {
  liveSandboxData.snapshot = { sandboxes: [{ ...CREATED, idle_suspend_after_seconds: 1800 }] };
  const { container } = await render();
  vi.spyOn(api, "PATCH").mockResolvedValue({
    error: { detail: "Test edit refused" },
    response: new Response(null, { status: 404 }),
  } as never);

  await type(idleInput(container), "45");
  await press(idleInput(container), "Enter");

  expect(idleInput(container).value).toBe("30");
  expect(container.textContent).toContain("Test edit refused");
});
