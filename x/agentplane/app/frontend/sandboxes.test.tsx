// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import { api, type ActionPolicySetView, type SandboxPresetView } from "./client";
import { SandboxList } from "./sandboxes";

vi.mock("./live", () => ({
  liveSandboxesUrl: () => "/live/sandboxes",
  useLive: () => ({ snapshot: null }),
  LiveStatus: () => null,
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

async function render(codexModels: string[] = ["test-codex-a", "test-codex-b"]): Promise<HTMLDivElement> {
  const preset: SandboxPresetView = {
    name: "test-preset",
    title: "Test preset",
    template: "test-template",
    policies: [],
    action_policy_sets: ["test-reads"],
    thread_preset: "test-thread",
    thread_defaults: { provider: "codex", model: "test-codex-b" },
  };
  const policySets: ActionPolicySetView[] = [
    {
      name: "test-reads",
      generation: 1,
      ready: { status: "True", reason: "Valid", message: "spec accepted", observed_generation: 1 },
      refused: null,
    },
    { name: "test-broken", generation: 1, ready: null, refused: "spec.autoApproveIf.0: unknown" },
  ];
  vi.spyOn(api, "GET").mockImplementation(async (path) => {
    const data =
      path === "/models"
        ? { claude: ["test-claude"], codex: codexModels }
        : path === "/presets"
          ? [preset]
          : path === "/action-policy/sets"
            ? policySets
            : [];
    return { data, response: new Response() } as Awaited<ReturnType<typeof api.GET>>;
  });
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () =>
    root.render(
      <MantineProvider>
        <MemoryRouter>
          <SandboxList onOpen={vi.fn()} />
        </MemoryRouter>
      </MantineProvider>
    )
  );
  await choose(container, "Preset", "Test preset");
  return container;
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
  const container = await render();
  expect(input(container, "Model").value).toBe("test-codex-b");
  await choose(container, "Model", "test-codex-a");
  expect(input(container, "Model").value).toBe("test-codex-a");
  await choose(container, "Harness", "claude");
  expect(input(container, "Model").value).toBe("test-claude");
  await act(async () => input(container, "Model").click());
  expect(options(container, "Model").map((node) => node.textContent)).toEqual(["test-claude"]);
});

it("pre-fills the preset's action policy sets, offers every set with its verdict, and sends the pick", async () => {
  const container = await render();
  expect(input(container, "Action policy sets").value).toBe("");
  expect(container.textContent).toContain("test-reads");
  await act(async () => input(container, "Action policy sets").click());
  expect(options(container, "Action policy sets").map((node) => node.textContent)).toEqual([
    "test-reads",
    "test-broken · invalid",
  ]);
  await act(async () => input(container, "Action policy sets").click());
  const post = vi.spyOn(api, "POST").mockResolvedValue({ data: {}, response: new Response() } as never);
  await type(input(container, "Name"), "picked");
  const button = [...container.querySelectorAll("button")].find((node) => node.textContent === "New sandbox");
  if (!button) throw new Error("Missing New sandbox button");
  await act(async () => button.click());
  expect(post).toHaveBeenCalledWith(
    "/sandboxes",
    expect.objectContaining({
      body: expect.objectContaining({ preset: "test-preset", action_policy_sets: ["test-reads"] }),
    })
  );
});

it("clears an unavailable preset model and disables a harness with no offered models", async () => {
  const container = await render([]);
  expect(input(container, "Model").value).toBe("");
  expect(input(container, "Model").disabled).toBe(true);
  expect(input(container, "Model").placeholder).toBe("No models available");
  await choose(container, "Harness", "claude");
  expect(input(container, "Model").disabled).toBe(false);
  expect(input(container, "Model").value).toBe("test-claude");
});
