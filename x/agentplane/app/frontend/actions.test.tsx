// @vitest-environment happy-dom

import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ActionRequests, stateLabel } from "./actions";
import { actionService, type ActionRequestView, type ActionService, type ActionState } from "./client";

const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function request(state: ActionState, index: number): ActionRequestView {
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

async function render(service: ActionService): Promise<HTMLDivElement> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () => {
    root.render(
      <MantineProvider>
        <ActionRequests service={service} />
      </MantineProvider>
    );
  });
  return container;
}

function button(container: HTMLElement, label: string): HTMLButtonElement {
  const found = [...container.querySelectorAll("button")].find((candidate) => candidate.textContent?.includes(label));
  if (!(found instanceof HTMLButtonElement)) throw new Error(`missing ${label} button`);
  return found;
}

afterEach(async () => {
  for (const item of mounted.splice(0)) {
    await act(async () => item.root.unmount());
    item.container.remove();
  }
});

describe("ActionRequests", () => {
  it("shows structured list errors without an empty-state claim", async () => {
    const failure = { detail: { code: "operator_federation_exchange_failed" } };
    const container = await render({ list: vi.fn().mockRejectedValue(failure), decide: vi.fn() });
    expect(container.textContent).toContain(JSON.stringify(failure));
    expect(container.textContent).not.toContain("[object Object]");
    expect(container.textContent).not.toContain("No requests are waiting");
    expect(container.textContent).not.toContain("No decided requests");
    expect(container.textContent).not.toContain("Loading actions");
  });

  it.each(["decision_pending", "succeeded"] as const)(
    "shows the immutable authenticated submitter for %s receipts",
    async (state) => {
      const grant = {
        identity_id: "test_identity",
        issuer: "https://test-issuer.example/oauth",
        client_id: "test-external-client",
        connection_id: "40000000-0000-4000-8000-000000000001",
        grant_id: "50000000-0000-4000-8000-000000000001",
        revision: 7,
      };
      const row = {
        ...request(state, 1),
        caller_principal: "configured-identity:test_identity",
        external_grant: grant,
        origin: { identity_id: "forged-origin-identity", client_id: "forged-origin-client" },
        correlation: { connection_id: "forged-correlation-connection", display_name: "mutable-connection-name" },
      };
      const container = await render({ list: async () => [row], decide: vi.fn() });

      expect(container.textContent).toContain("Authenticated external caller at submission");
      for (const value of [grant.identity_id, grant.issuer, grant.client_id, grant.connection_id]) {
        expect(container.textContent).toContain(value);
      }
      expect(container.textContent).not.toContain("forged-");
      expect(container.textContent).not.toContain("mutable-connection-name");
      const details = container.querySelector("details");
      const summary = details?.querySelector("summary");
      if (!details || !summary) throw new Error("missing grant audit disclosure");
      expect(details.open).toBe(false);
      await act(async () => summary.click());
      expect(details.open).toBe(true);
      expect(details.textContent).toContain(grant.grant_id);
      expect(details.textContent).toContain("Revision 7");
      expect(details.textContent).toContain("Historical submission evidence");
    }
  );

  it.each([null, undefined])(
    "retains workload caller display without manufacturing external provenance (%s)",
    async (external_grant) => {
      const row = {
        ...request("decision_pending", 1),
        external_grant,
        origin: { identity_id: "forged-origin-identity" },
      };
      const container = await render({ list: async () => [row], decide: vi.fn() });
      expect(container.textContent).toContain(row.caller_principal);
      expect(container.textContent).not.toContain("Authenticated external caller");
      expect(container.textContent).not.toContain("forged-origin-identity");
      expect(container.querySelector("details")).toBeNull();
      expect(button(container, "Allow").disabled).toBe(false);
    }
  );

  it("renders pending, decision, running, and every terminal outcome", async () => {
    const states: ActionState[] = [
      "decision_pending",
      "allowed",
      "denied",
      "dispatching",
      "running",
      "succeeded",
      "failed",
      "cancelled",
      "execution_unknown",
    ];
    const service: ActionService = {
      list: vi.fn(async () => states.map(request)),
      decide: vi.fn(),
    };

    const container = await render(service);

    for (const state of states) expect(container.textContent).toContain(stateLabel(state));
    expect(container.textContent).toContain("Exact arguments (unredacted)");
    expect(container.textContent).toContain("test-exact-token");
    expect(container.textContent).toContain("test-exact-password");
    expect(container.textContent).toContain("Reviewed scope — allowed for this request.");
    expect(container.textContent).toContain("Result");
    expect(container.textContent).toContain("Execution error");
  });

  it.each([
    ["Allow", "allow", "allowed"],
    ["Deny", "deny", "denied"],
  ] as const)("sends a human %s decision and replaces the pending receipt", async (label, verdict, state) => {
    let rows = [request("decision_pending", 1)];
    const decide = vi.fn(async (pending: ActionRequestView) => {
      rows = [{ ...pending, state, version: 2 }];
      return rows[0];
    });
    const service: ActionService = { list: vi.fn(async () => rows), decide };
    const container = await render(service);

    await act(async () => button(container, label).click());

    expect(decide).toHaveBeenCalledOnce();
    expect(decide).toHaveBeenCalledWith(expect.objectContaining({ state: "decision_pending" }), verdict);
    expect(container.textContent).toContain(state);
    expect(container.textContent).not.toContain("Pending (1)");
  });
});

it("renders server-pushed Action state without list polling and closes the stream", async () => {
  let stream: EventTarget | undefined;
  const close = vi.fn();
  class Stream extends EventTarget {
    onerror = null;
    close = close;
    constructor(url: string) {
      super();
      expect(url).toBe("/actions/stream");
      stream = this;
    }
  }
  vi.stubGlobal("EventSource", Stream);
  const list = vi.spyOn(actionService, "list").mockResolvedValue([]);
  try {
    const container = await render(actionService);
    expect(container.textContent).toContain("Loading actions");
    expect(container.textContent).not.toContain("No requests are waiting");
    expect(container.textContent).not.toContain("No decided requests");
    expect(container.textContent).not.toContain("Pending (0)");
    await act(async () => {
      stream?.dispatchEvent(new MessageEvent("snapshot", { data: "not JSON" }));
    });
    expect(container.textContent).toContain("The live Action update was invalid");
    expect(container.textContent).not.toContain("No requests are waiting");
    expect(container.textContent).not.toContain("No decided requests");
    await act(async () => {
      stream?.dispatchEvent(new MessageEvent("snapshot", { data: "[]" }));
    });
    expect(container.textContent).toContain("No requests are waiting");
    expect(container.textContent).toContain("No decided requests");
    expect(container.textContent).not.toContain("Loading actions");
    expect(container.textContent).not.toContain("The live Action update was invalid");
    await act(async () => {
      stream?.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify([request("decision_pending", 1)]) }));
    });
    expect(container.textContent).toContain("Pending (1)");
    await act(async () => {
      stream?.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify([request("denied", 1)]) }));
    });
    expect(container.textContent).toContain("Pending (0)");
    expect(container.textContent).toContain("denied");
    expect(list).not.toHaveBeenCalled();
    const item = mounted.pop();
    await act(async () => item?.root.unmount());
    item?.container.remove();
    expect(close).toHaveBeenCalledOnce();
  } finally {
    list.mockRestore();
    vi.unstubAllGlobals();
  }
});
