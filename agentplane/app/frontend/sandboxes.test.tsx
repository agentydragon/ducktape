// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { TEST_REASONING_EFFORTS, testModelCatalog } from "./test_model_catalog";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import { api, type SandboxPresetView, type SandboxView } from "./client";
import { SandboxList } from "./sandboxes";
import { deletable } from "./lifecycle";
import { sandboxSummary } from "./sandbox_status";

vi.mock("./live", () => ({
  liveSandboxesUrl: () => "/live/sandboxes",
  useLive: () => ({ snapshot: null }),
}));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];
afterEach(async () => {
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
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
  deleting: false,
  pod: null,
};

async function render(
  codexModels: { model: string; display_name: string; reasoning_efforts: string[] }[] = [
    { model: "test-codex-a", display_name: "Test Codex A", reasoning_efforts: TEST_REASONING_EFFORTS },
    { model: "test-codex-b", display_name: "Test Codex B", reasoning_efforts: TEST_REASONING_EFFORTS },
  ]
) {
  const onOpen = vi.fn();
  const preset: SandboxPresetView = {
    name: "test-preset",
    title: "Test preset",
    template: "test-template",
    policy_sets: ["test-basic"],
    session_defaults: { harness: "HARNESS_CODEX", model: "test-codex-b" },
    bootstrap: "mkdir -p /state/workspaces",
  };
  vi.spyOn(api, "GET").mockImplementation(async (path) => {
    const data =
      path === "/models"
        ? testModelCatalog(
            [{ model: "test-claude", display_name: "Test Claude", reasoning_efforts: TEST_REASONING_EFFORTS }],
            codexModels
          )
        : path === "/presets"
          ? [preset]
          : path === "/sandboxes/form"
            ? { policy_sets: ["test-basic", "test-packages"] }
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

it("inherits the preset model and replaces incompatible choices when the harness changes", async () => {
  const { container } = await render();
  expect(input(container, "Sandbox template").value).toBe("test-template");
  expect(input(container, "Model").value).toBe("Test Codex B");
  await choose(container, "Model", "Test Codex A");
  expect(input(container, "Model").value).toBe("Test Codex A");
  await choose(container, "Harness", "Claude");
  expect(input(container, "Model").value).toBe("Test Claude");
  await act(async () => input(container, "Model").click());
  expect(options(container, "Model").map((node) => node.textContent)).toEqual(["Test Claude"]);
});

it("pre-fills the preset's policy sets, hides picked sets, and sends the picks", async () => {
  const { container, onOpen } = await render();
  expect(container.textContent).toContain("test-basic");
  await act(async () => input(container, "Policy sets").click());
  expect(options(container, "Policy sets").map((node) => node.textContent)).toEqual(["test-packages"]);
  await act(async () => input(container, "Policy sets").click());
  await choose(container, "Policy sets", "test-packages");
  const post = vi.spyOn(api, "POST").mockResolvedValue({ data: CREATED, response: new Response() } as never);
  await type(input(container, "Name"), "picked");
  const button = [...container.querySelectorAll("button")].find((node) => node.textContent === "New sandbox");
  if (!button) throw new Error("Missing New sandbox button");
  await act(async () => button.click());
  expect(post).toHaveBeenCalledWith(
    "/sandboxes",
    expect.objectContaining({
      body: expect.objectContaining({
        template: "test-template",
        policy_sets: ["test-basic", "test-packages"],
        bootstrap: "mkdir -p /state/workspaces",
        session_defaults: expect.objectContaining({ harness: "HARNESS_CODEX", model: "test-codex-b" }),
      }),
    })
  );
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

it("replaces an unavailable preset harness and disables its option", async () => {
  const { container } = await render([]);
  expect(input(container, "Harness").value).toBe("Claude");
  expect(input(container, "Model").value).toBe("Test Claude");
  await act(async () => input(container, "Harness").click());
  const codex = options(container, "Harness").find((option) => option.textContent === "Codex (no models offered)");
  expect(codex?.hasAttribute("data-combobox-disabled")).toBe(true);
  await act(async () => codex!.click());
  expect(input(container, "Harness").value).toBe("Claude");
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
  const suspended: SandboxView = { ...CREATED, operating_mode: "Suspended" };
  expect(sandboxSummary(suspended)).toEqual({ label: "Suspended", kind: "suspended" });
  expect(deletable(suspended)).toBe(true);
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

it("flags an invalid typed name and refuses to create", async () => {
  const { container } = await render();
  await type(input(container, "Name"), "Bad_Name");
  expect(input(container, "Name").getAttribute("aria-invalid")).toBe("true");
  const button = [...container.querySelectorAll("button")].find((node) => node.textContent === "New sandbox");
  expect(button?.hasAttribute("disabled")).toBe(true);
});
