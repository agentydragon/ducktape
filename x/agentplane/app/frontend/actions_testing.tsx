import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach } from "vitest";

import type { ActionRequestView, ActionService, ActionState } from "./client";

/** Shared fixture/mount plumbing for the pending (`actions.test.tsx`) and history
 * (`actions_history.test.tsx`) suites, which exercise the same `ActionRequestView` shape and the
 * same live-stream/list mounting behavior against two different views. */
export type View = (props: { service?: ActionService }) => JSX.Element;

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

export function request(state: ActionState, index: number): ActionRequestView {
  const decided = state !== "decision_pending";
  const executing = !["decision_pending", "allowed", "denied"].includes(state);
  return {
    id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
    action: { group: "agentplane", name: `state-${state}` },
    arguments: { state, token: "test-exact-token", nested: { password: "test-exact-password" } },
    origin: { thread_id: "10000000-0000-4000-8000-000000000000" },
    correlation: {},
    idempotency_key: `request-${index}`,
    caller_principal: "system:serviceaccount:test:agent",
    state,
    version: decided ? 2 : 1,
    created_at: "2026-09-05T12:00:00Z",
    updated_at: "2026-09-05T12:00:00Z",
    decision: decided
      ? {
          id: `20000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
          verdict: state === "denied" ? "deny" : "allow",
          provider: "human_operator",
          issuer: "operator",
          decision_note: "Reviewed scope — allowed for this request.",
          idempotency_key: `decision-${index}`,
          decided_at: "2026-09-05T12:00:00Z",
        }
      : null,
    execution: executing
      ? {
          id: `30000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
          state: state as NonNullable<ActionRequestView["execution"]>["state"],
          result: state === "succeeded" ? { ok: true } : null,
          error: ["failed", "cancelled", "execution_unknown"].includes(state) ? { kind: state } : null,
          created_at: "2026-09-05T12:00:00Z",
          started_at: "2026-09-05T12:00:01Z",
          reconciled_at: null,
          completed_at: ["running", "dispatching"].includes(state) ? null : "2026-09-05T12:00:02Z",
        }
      : null,
  };
}

const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];

export async function render(service: ActionService, View: View): Promise<HTMLDivElement> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () => {
    root.render(
      <MantineProvider>
        <View service={service} />
      </MantineProvider>
    );
  });
  return container;
}

export function button(container: HTMLElement, label: string): HTMLButtonElement {
  const found = [...container.querySelectorAll("button")].find((candidate) => candidate.textContent?.includes(label));
  if (!(found instanceof HTMLButtonElement)) throw new Error(`missing ${label} button`);
  return found;
}

/** Unmounts the most recently `render()`ed view now, for a test that asserts cleanup behavior
 * (e.g. a live stream's `close()`) rather than waiting for the automatic `afterEach` below. */
export async function unmountLast(): Promise<void> {
  const item = mounted.pop();
  if (!item) return;
  await act(async () => item.root.unmount());
  item.container.remove();
}

afterEach(async () => {
  for (const item of mounted.splice(0)) {
    await act(async () => item.root.unmount());
    item.container.remove();
  }
});
