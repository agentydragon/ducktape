// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it } from "vitest";

import { ActionPolicySection } from "./action_policy";
import type { ActionPolicyView } from "./client";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

async function render(policy: ActionPolicyView | null): Promise<void> {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <MantineProvider>
        <ActionPolicySection policy={policy} />
      </MantineProvider>
    )
  );
}

const EMPTY: ActionPolicyView = { bindings: [], auto_approve_if: [], auto_deny_if: [], auto_deny_unless: [] };

it("says an unbound sandbox waits for the operator on every Action", async () => {
  await render(EMPTY);
  expect(container.textContent).toContain("No binding names this sandbox");
  expect(container.textContent).toContain("every Action from this sandbox waits for the operator");
});

it("renders nothing until the stream has delivered a snapshot", async () => {
  await render(null);
  // MantineProvider contributes a <style> of its own, so the check is for content, not text.
  expect(container.querySelector("table, h5")).toBeNull();
});

it("shows each set's state, the missing one, and the effective lists in evaluation order", async () => {
  await render({
    bindings: [
      {
        name: "test-sandbox-launch",
        provenance: "app",
        expires_at: null,
        ready: { status: "True", reason: "Valid", message: "spec accepted", observed_generation: 1 },
        policy_sets: [
          {
            name: "test-reads",
            generation: 3,
            ready: { status: "True", reason: "Valid", message: "spec accepted", observed_generation: 2 },
            refused: null,
          },
          {
            name: "test-broken",
            generation: 1,
            ready: {
              status: "False",
              reason: "Invalid",
              message: "spec.autoApproveIf.0: unknown",
              observed_generation: 1,
            },
            refused: "spec.autoApproveIf.0: unknown",
          },
        ],
        missing_policy_sets: ["test-vanished"],
      },
    ],
    auto_approve_if: [
      {
        binding: "test-sandbox-launch",
        policy_set: "test-reads",
        index: 1,
        policy: {
          type: "argument_schema",
          actions: { github: ["create_issue"] },
          argument_schema: { properties: { owner: { const: "test-owner" } } },
        },
      },
    ],
    auto_deny_if: [],
    auto_deny_unless: [],
  });
  const text = container.textContent ?? "";
  expect(text).toContain("test-sandbox-launch");
  expect(text).toContain("app, at launch");
  expect(text).toContain("test-broken: invalid");
  expect(text).toContain("test-vanished?");
  // The set's spec moved on from the generation the service judged.
  expect(text).toContain("edited");
  expect(text).toContain("test-sandbox-launch · test-reads[1]");
  expect(text).toContain("github: create_issue");
  expect(text).toContain('"owner"');
  expect(text).toContain("not yet enforced");
});
