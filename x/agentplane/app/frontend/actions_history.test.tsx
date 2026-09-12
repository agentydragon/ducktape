// @vitest-environment happy-dom

import { act } from "react";
import { describe, expect, it, vi } from "vitest";

import { stateLabel } from "./actions";
import { ActionHistory } from "./actions_history";
import { render, request, unmountLast } from "./actions_testing";
import { actionService, type ActionRequestView, type ActionService, type ActionState } from "./client";

describe("ActionHistory", () => {
  it("shows structured list errors without an empty-state claim", async () => {
    const failure = { detail: { code: "operator_federation_exchange_failed" } };
    const container = await render({ list: vi.fn().mockRejectedValue(failure), decide: vi.fn() }, ActionHistory);
    expect(container.textContent).toContain(JSON.stringify(failure));
    expect(container.textContent).not.toContain("[object Object]");
    expect(container.textContent).not.toContain("No decided requests");
    expect(container.textContent).not.toContain("Loading actions");
  });

  it("shows the immutable authenticated submitter for a decided receipt", async () => {
    const grant = {
      caller: { namespace: "agentplane-test", name: "test-caller" },
      issuer: "https://test-issuer.example/oauth",
      client_id: "test-external-client",
      connection_id: "40000000-0000-4000-8000-000000000001",
      grant_id: "50000000-0000-4000-8000-000000000001",
      revision: 7,
    };
    const row = {
      ...request("succeeded", 1),
      caller_principal: "service-account:agentplane-test:test-caller",
      external_grant: grant,
    };
    const container = await render({ list: async () => [row], decide: vi.fn() }, ActionHistory);

    expect(container.textContent).toContain("Authenticated external caller at submission");
    for (const value of ["agentplane-test/test-caller", grant.issuer, grant.client_id, grant.connection_id]) {
      expect(container.textContent).toContain(value);
    }
    const details = container.querySelector("details");
    const summary = details?.querySelector("summary");
    if (!details || !summary) throw new Error("missing grant audit disclosure");
    expect(details.open).toBe(false);
    await act(async () => summary.click());
    expect(details.open).toBe(true);
    expect(details.textContent).toContain(grant.grant_id);
  });

  it("renders every terminal outcome with its decision, result, and error", async () => {
    const states: ActionState[] = [
      "allowed",
      "denied",
      "dispatching",
      "running",
      "succeeded",
      "failed",
      "cancelled",
      "execution_unknown",
    ];
    const service: ActionService = { list: vi.fn(async () => states.map(request)), decide: vi.fn() };

    const container = await render(service, ActionHistory);

    for (const state of states) expect(container.textContent).toContain(stateLabel(state));
    expect(container.textContent).toContain("requested by system:serviceaccount:test:agent");
    expect(container.textContent).toContain("Allowed");
    expect(container.textContent).toContain("Denied");
    expect(container.textContent).toContain("Reviewed scope — allowed for this request.");
    expect(container.textContent).toContain("Result");
    expect(container.textContent).toContain("Execution error");
    // Arguments stay in the DOM (Mantine's Collapse animates height rather than unmounting) but
    // start folded, per the disclosure convention shared with the session transcript.
    expect(container.textContent).toContain("test-exact-token");
  });

  it("folds Arguments behind the shared disclosure convention until expanded", async () => {
    const container = await render({ list: async () => [request("succeeded", 1)], decide: vi.fn() }, ActionHistory);
    const control = [...container.querySelectorAll("button")].find((candidate) =>
      candidate.textContent?.includes("Arguments")
    );
    if (!control) throw new Error("missing Arguments disclosure control");
    expect(control.getAttribute("aria-expanded")).toBe("false");
    await act(async () => control.click());
    expect(control.getAttribute("aria-expanded")).toBe("true");
  });

  it("renders the auto-approving policy when the Decision carries policy evidence", async () => {
    const base = request("succeeded", 1);
    const row: ActionRequestView = {
      ...base,
      decision: {
        ...base.decision!,
        provider: "policy_engine",
        decision_note: null,
        policy_evidence: {
          bindings: [{ namespace: "agentplane-visual", name: "demo-a1b2-github-public", resource_version: "12345" }],
          policy_sets: [{ namespace: "agentplane-visual", name: "fixture-auto-allow", generation: 1 }],
          matched: {
            namespace: "agentplane-visual",
            policy_set: "fixture-auto-allow",
            source: "autoApproveIf",
            index: 0,
            type: "exact_actions",
          },
        },
      },
    };
    const container = await render({ list: async () => [row], decide: vi.fn() }, ActionHistory);
    expect(container.textContent).toContain("Auto-approved via policy");
    expect(container.textContent).toContain("fixture-auto-allow");
  });

  it("renders server-pushed decided state without list polling and closes the stream", async () => {
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
      const container = await render(actionService, ActionHistory);
      expect(container.textContent).toContain("Loading actions");
      await act(async () => {
        stream?.dispatchEvent(new MessageEvent("snapshot", { data: "[]" }));
      });
      expect(container.textContent).toContain("No decided requests");
      await act(async () => {
        stream?.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify([request("denied", 1)]) }));
      });
      expect(container.textContent).not.toContain("No decided requests");
      expect(container.textContent).toContain("Denied");
      expect(list).not.toHaveBeenCalled();
      await unmountLast();
      expect(close).toHaveBeenCalledOnce();
    } finally {
      list.mockRestore();
      vi.unstubAllGlobals();
    }
  });
});
