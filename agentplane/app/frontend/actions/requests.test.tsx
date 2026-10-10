// @vitest-environment happy-dom

import { act } from "react";
import { describe, expect, it, vi } from "vitest";

import { STALE_AFTER_MS } from "../stream_status";
import { actionService, type ActionRequestView, type ActionService } from "./client";
import { ActionRequests, stateLabel } from "./requests";
import { button, render, request, SSH_EXEC_ARGUMENTS, sshExec, unmountLast } from "./testing";

describe("ActionRequests", () => {
  it("shows structured list errors without an empty-state claim", async () => {
    const failure = { detail: { code: "operator_federation_exchange_failed" } };
    const container = await render({ list: vi.fn().mockRejectedValue(failure), decide: vi.fn() }, ActionRequests);
    expect(container.textContent).toContain(JSON.stringify(failure));
    expect(container.textContent).not.toContain("[object Object]");
    expect(container.textContent).not.toContain("No requests are waiting");
    expect(container.textContent).not.toContain("Loading actions");
  });

  it("shows the immutable authenticated submitter for a pending receipt", async () => {
    const grant = {
      caller: { namespace: "agentplane-test", name: "test-caller" },
      issuer: "https://test-issuer.example/oauth",
      client_id: "test-external-client",
      connection_id: "40000000-0000-4000-8000-000000000001",
      grant_id: "50000000-0000-4000-8000-000000000001",
      revision: 7,
      binding_version: 0,
    };
    const row = {
      ...request("decision_pending", 1),
      caller: { namespace: "agentplane-test", name: "test-caller" },
      external_grant: grant,
    };
    const container = await render({ list: async () => [row], decide: vi.fn() }, ActionRequests);

    expect(container.textContent).toContain("requested by agentplane-test/test-caller");
    expect(container.textContent).toContain("Authenticated external caller at submission");
    for (const value of [grant.issuer, grant.client_id, grant.connection_id]) {
      expect(container.textContent).toContain(value);
    }
    const control = container.querySelector<HTMLButtonElement>(".agentplane-disclosure-summary");
    if (!control) throw new Error("missing grant audit disclosure");
    expect(control.getAttribute("aria-expanded")).toBe("false");
    await act(async () => control.click());
    expect(control.getAttribute("aria-expanded")).toBe("true");
    expect(container.textContent).toContain(grant.grant_id);
    expect(container.textContent).toContain("Revision 7");
    expect(container.textContent).toContain("Historical submission evidence");
  });

  it.each([null, undefined])(
    "retains workload caller display without manufacturing external provenance (%s)",
    async (external_grant) => {
      const row = {
        ...request("decision_pending", 1),
        external_grant,
      };
      const container = await render({ list: async () => [row], decide: vi.fn() }, ActionRequests);
      expect(container.textContent).toContain(`requested by ${row.caller!.namespace}/${row.caller!.name}`);
      expect(container.textContent).not.toContain("Authenticated external caller");
      // The request-id disclosure exists regardless of external_grant, but carries only the id --
      // no grant provenance to fold in without one.
      const control = container.querySelector<HTMLButtonElement>(".agentplane-disclosure-summary");
      expect(container.textContent).not.toContain("Authenticated external caller");
      expect(control?.textContent).toBe("Request audit details");
      expect(control?.getAttribute("aria-expanded")).toBe("false");
      expect(container.textContent).toContain(row.id);
      expect(button(container, "Approve").disabled).toBe(false);
    }
  );

  it("renders every pending request with its unredacted arguments", async () => {
    const service: ActionService = { list: vi.fn(async () => [request("decision_pending", 1)]), decide: vi.fn() };
    const container = await render(service, ActionRequests);
    expect(container.textContent).not.toContain(stateLabel("decision_pending"));
    expect(button(container, "Approve").getAttribute("aria-label")).toBe("Approve");
    expect(button(container, "Deny").getAttribute("aria-label")).toBe("Deny");
    expect(button(container, "Approve").querySelector("svg")).not.toBeNull();
    expect(button(container, "Deny").querySelector("svg")).not.toBeNull();
    expect(container.textContent).toContain("Exact arguments (unredacted)");
    expect(container.textContent).toContain("test-exact-token");
    expect(container.textContent).toContain("test-exact-password");
  });

  it("draws a registered Action's arguments with its widget, and the card's Raw switch turns them to JSON", async () => {
    const container = await render(
      { list: async () => [sshExec("decision_pending")], decide: vi.fn() },
      ActionRequests
    );
    expect(container.textContent).toContain("test-user@test-host.example");
    expect(container.textContent).not.toContain('"command"');
    const raw = container.querySelector<HTMLInputElement>('input[type="checkbox"]');
    if (!raw) throw new Error("missing the Raw switch");
    await act(async () => raw.click());
    expect(container.textContent).toContain('"command": "echo test-output"');
    expect(container.textContent).not.toContain("test-user@test-host.example");
  });

  it("uses a human-facing Action label instead of the technical group and name", async () => {
    const row = {
      ...request("decision_pending", 1),
      action: { group: "kubernetes_admin", name: "pods_list_in_namespace" },
      arguments: { namespace: "tofu-controller" },
    };
    const container = await render({ list: async () => [row], decide: vi.fn() }, ActionRequests);

    expect(container.textContent).toContain("List pods in namespace tofu-controller");
    expect(container.textContent).not.toContain("kubernetes_admin / pods_list_in_namespace");
  });

  it("offers no Raw switch where the arguments show only as their JSON", async () => {
    // Arguments no widget is registered for, and ones the registered widget does not take.
    const unfit = {
      ...sshExec("decision_pending"),
      id: request("decision_pending", 2).id,
      arguments: { ...SSH_EXEC_ARGUMENTS, test_extra: "test-value" },
    };
    const container = await render(
      { list: async () => [request("decision_pending", 1), unfit], decide: vi.fn() },
      ActionRequests
    );
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
    expect(container.textContent).toContain('"test_extra": "test-value"');
  });

  it("renders the caller's own title and description verbatim, as plain text", async () => {
    const row = {
      ...request("decision_pending", 1),
      title: "delete the <b>crashlooping</b> test pod",
      description: "Restarted 14 times in *5* minutes; the rest of the test deployment is healthy.",
    };
    const container = await render({ list: async () => [row], decide: vi.fn() }, ActionRequests);
    expect(container.textContent).toContain(row.title);
    expect(container.textContent).toContain(row.description);
    expect(container.innerHTML).not.toContain("<b>");
    expect(container.querySelector("em")).toBeNull();
  });

  it("renders a request whose caller supplied no description", async () => {
    const row = { ...request("decision_pending", 1), description: null };
    const container = await render({ list: async () => [row], decide: vi.fn() }, ActionRequests);
    expect(container.textContent).toContain(row.title);
    expect(container.textContent).not.toContain("null");
  });

  it.each([
    ["Approve", "allow", "allowed"],
    ["Deny", "deny", "denied"],
  ] as const)("sends a human %s decision and replaces the pending receipt", async (label, verdict, state) => {
    let rows = [request("decision_pending", 1)];
    const decide = vi.fn(async (pending: ActionRequestView) => {
      rows = [{ ...pending, state, version: 2 }];
      return rows[0];
    });
    const service: ActionService = { list: vi.fn(async () => rows), decide };
    const container = await render(service, ActionRequests);

    await act(async () => button(container, label).click());

    expect(decide).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ state: "decision_pending" }), verdict);
    expect(container.textContent).not.toContain("Pending (1)");
  });

  it("renders server-pushed pending state without list polling and closes the stream", async () => {
    const stream: { current?: EventTarget } = {};
    const close = vi.fn();
    class Stream extends EventTarget {
      onerror = null;
      close = close;
      constructor(url: string) {
        super();
        expect(url).toBe("/actions/stream?state=decision_pending");
        stream.current = this;
      }
    }
    vi.stubGlobal("EventSource", Stream);
    const list = vi.spyOn(actionService, "list").mockResolvedValue([]);
    try {
      const container = await render(actionService, ActionRequests);
      expect(container.textContent).toContain("Loading actions");
      expect(container.textContent).not.toContain("No requests are waiting");
      expect(container.textContent).not.toContain("Pending (0)");
      await act(async () => {
        stream.current?.dispatchEvent(new MessageEvent("snapshot", { data: "not JSON" }));
      });
      expect(container.textContent).toContain("The live Action update was invalid");
      await act(async () => {
        stream.current?.dispatchEvent(new MessageEvent("snapshot", { data: "[]" }));
      });
      expect(container.textContent).toContain("No requests are waiting");
      expect(container.textContent).not.toContain("Loading actions");
      await act(async () => {
        stream.current?.dispatchEvent(
          new MessageEvent("snapshot", { data: JSON.stringify([request("decision_pending", 1)]) })
        );
      });
      expect(container.textContent).toContain("Pending (1)");
      await act(async () => {
        stream.current?.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify([request("denied", 1)]) }));
      });
      expect(container.textContent).toContain("Pending (0)");
      expect(list).not.toHaveBeenCalled();
      await unmountLast();
      expect(close).toHaveBeenCalledOnce();
    } finally {
      list.mockRestore();
      vi.unstubAllGlobals();
    }
  });

  it("keeps a dropped stream's requests without comment until it has been down a minute", async () => {
    vi.useFakeTimers();
    const stream: { current?: EventTarget } = {};
    class Stream extends EventTarget {
      // A drop is the network's, which the browser retries: the source stays CONNECTING.
      readyState = 0;
      close = vi.fn();
      constructor() {
        super();
        stream.current = this;
      }
    }
    vi.stubGlobal("EventSource", Stream);
    try {
      const container = await render(actionService, ActionRequests);
      await act(async () => {
        stream.current?.dispatchEvent(
          new MessageEvent("snapshot", { data: JSON.stringify([request("decision_pending", 1)]) })
        );
        stream.current?.dispatchEvent(new Event("error"));
      });
      expect(container.querySelector('[role="alert"]')).toBeNull();
      expect(container.textContent).toContain("Pending (1)");

      await act(async () => vi.advanceTimersByTime(STALE_AFTER_MS));
      expect(container.querySelector('[role="alert"]')).not.toBeNull();
      expect(container.textContent).toContain("Pending (1)");
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });
});
