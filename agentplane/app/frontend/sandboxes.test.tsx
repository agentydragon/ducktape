// @vitest-environment happy-dom
import { screen, waitFor, within } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { TEST_REASONING_EFFORTS, testModelCatalog } from "./test_model_catalog";
import { afterEach, expect, it, vi } from "vitest";

import { api, type SandboxPresetView, type SandboxView } from "./client";
import type { ActionPolicySetView } from "./actions/client";
import { SandboxList } from "./sandboxes";
import { renderInMantine } from "./testing_library";

vi.mock("./live", () => ({
  liveSandboxesUrl: () => "/live/sandboxes",
  useLive: () => ({ snapshot: null }),
  LiveStatus: () => null,
}));

afterEach(() => vi.restoreAllMocks());

const CREATED: SandboxView = {
  name: "test-created-sandbox",
  uid: "00000000-0000-4000-8000-000000000001",
  namespace: "agentplane-test",
  created_at: "2026-01-01T00:00:00Z",
  operating_mode: "Running",
  service_account: { namespace: "agentplane-test", name: "test-created-sandbox" },
  status: null,
  kubernetes_grants: [],
  kubernetes_grants_ready: true,
  kubernetes_grant_error: null,
  launch_grants_pending: false,
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
    policies: [],
    action_policy_sets: ["test-reads"],
    kubernetes_grants: ["workspace-read"],
    session_defaults: { harness: "HARNESS_CODEX", model: "test-codex-b" },
    bootstrap: "mkdir -p /state/workspaces",
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
        ? testModelCatalog(
            [{ model: "test-claude", display_name: "Test Claude", reasoning_efforts: TEST_REASONING_EFFORTS }],
            codexModels
          )
        : path === "/presets"
          ? [preset]
          : path === "/sandboxes/templates"
            ? ["test-template", "other-template"]
            : path === "/action-policy/sets"
              ? policySets
              : path === "/kubernetes-grants"
                ? [
                    {
                      name: "workspace-read",
                      kind: "RoleBinding",
                      namespace: "agentplane-test",
                      role_ref: { kind: "Role", name: "workspace-reader" },
                    },
                  ]
                : [];
    return { data, response: new Response() } as Awaited<ReturnType<typeof api.GET>>;
  });
  const rendered = renderInMantine(
    <MemoryRouter>
      <SandboxList onOpen={onOpen} />
    </MemoryRouter>
  );
  await choose(rendered.user, "Preset", "Test preset");
  return Object.assign(rendered, { onOpen });
}

const field = (label: string): HTMLElement => screen.getByRole("combobox", { name: label });

/** The options a dropdown offers. Mantine renders it in a portal, which a `screen` query reaches. */
function options(label: string): HTMLElement[] {
  return within(screen.getByRole("listbox", { name: label })).getAllByRole("option");
}

async function choose(user: UserEvent, label: string, value: string): Promise<void> {
  await user.click(field(label));
  await user.click(await screen.findByRole("option", { name: value }));
}

async function createSandbox(user: UserEvent, name: string): Promise<void> {
  // Pasted, not typed: each keystroke re-renders the whole creation form.
  await user.click(screen.getByRole("textbox", { name: "Name" }));
  await user.paste(name);
  await user.click(screen.getByRole("button", { name: "New sandbox" }));
}

it("inherits the preset model and replaces incompatible choices when the harness changes", async () => {
  const { user } = await render();
  expect(field("Template")).toHaveValue("test-template");
  expect(field("Model")).toHaveValue("Test Codex B");
  await choose(user, "Model", "Test Codex A");
  expect(field("Model")).toHaveValue("Test Codex A");
  await choose(user, "Harness", "Claude");
  expect(field("Model")).toHaveValue("Test Claude");
  await user.click(field("Model"));
  expect(options("Model").map((node) => node.textContent)).toEqual(["Test Claude"]);
});

it("pre-fills the preset's policy sets and Kubernetes grants, shows role scope, and sends the picks", async () => {
  const { container, onOpen, user } = await render();
  expect(field("Action policy sets")).toHaveValue("");
  expect(container).toHaveTextContent("test-reads");
  expect(container).toHaveTextContent("workspace-read");
  expect(container).toHaveTextContent("Role/workspace-reader");
  expect(container).toHaveTextContent("namespace agentplane-test");
  await user.click(field("Action policy sets"));
  expect(options("Action policy sets").map((node) => node.textContent)).toEqual([
    "test-reads",
    "test-broken · invalid",
  ]);
  await user.click(field("Action policy sets"));
  const post = vi.spyOn(api, "POST").mockResolvedValue({ data: CREATED, response: new Response() } as never);
  await createSandbox(user, "picked");
  expect(post).toHaveBeenCalledWith(
    "/sandboxes",
    expect.objectContaining({
      body: expect.objectContaining({
        template: "test-template",
        action_policy_sets: ["test-reads"],
        kubernetes_grants: ["workspace-read"],
        bootstrap: "mkdir -p /state/workspaces",
        session_defaults: expect.objectContaining({ harness: "HARNESS_CODEX", model: "test-codex-b" }),
      }),
    })
  );
  await waitFor(() => expect(onOpen).toHaveBeenCalledExactlyOnceWith(CREATED.name));
});

it("allows an operator to remove the preset grant and sends an explicit empty grant list", async () => {
  const { user } = await render();
  // Mantine hides a pill's own remove button from the accessibility tree (`aria-hidden`); the keyboard
  // removes the last pill with Backspace.
  await user.click(field("Kubernetes grants"));
  await user.keyboard("{Backspace}");
  const post = vi.spyOn(api, "POST").mockResolvedValue({ data: CREATED, response: new Response() } as never);
  await createSandbox(user, "without-grant");
  expect(post).toHaveBeenCalledWith(
    "/sandboxes",
    expect.objectContaining({ body: expect.objectContaining({ kubernetes_grants: [] }) })
  );
});

it("lets an operator replace the preset template before creating the sandbox", async () => {
  const { user } = await render();
  await choose(user, "Template", "other-template");
  const post = vi.spyOn(api, "POST").mockResolvedValue({ data: CREATED, response: new Response() } as never);
  await createSandbox(user, "picked");

  expect(post).toHaveBeenCalledWith(
    "/sandboxes",
    expect.objectContaining({ body: expect.objectContaining({ template: "other-template" }) })
  );
});

it("replaces an unavailable preset harness and disables its option", async () => {
  const { user } = await render([]);
  expect(field("Harness")).toHaveValue("Claude");
  expect(field("Model")).toHaveValue("Test Claude");
  await user.click(field("Harness"));
  const codex = screen.getByRole("option", { name: "Codex (no models offered)" });
  // Mantine marks a disabled option only with this attribute, not `aria-disabled`.
  expect(codex).toHaveAttribute("data-combobox-disabled");
  await user.click(codex);
  expect(field("Harness")).toHaveValue("Claude");
});

it("keeps the creation form and reports rejection without navigating", async () => {
  const { onOpen, user } = await render();
  vi.spyOn(api, "POST").mockResolvedValue({
    error: { detail: "Test creation refused" },
    response: new Response(null, { status: 409 }),
  } as never);
  await createSandbox(user, "test-not-created");
  expect(await screen.findByText(/Test creation refused/)).toBeInTheDocument();
  expect(onOpen).not.toHaveBeenCalled();
  expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("test-not-created");
});
