// @vitest-environment happy-dom

import { act } from "react";
import { describe, expect, it, vi } from "vitest";

import { STALE_AFTER_MS, streamRegistry } from "../stream_status";
import { ActionRequests, stateLabel } from "./requests";
import {
  TestEventSource,
  answerApi,
  button,
  mount,
  renderLive,
  request,
  send,
  SSH_EXEC_ARGUMENTS,
  sshExec,
  unmountLast,
} from "./testing";

describe("ActionRequests", () => {
  it("shows a refused decision's structured error", async () => {
    const failure = { detail: { code: "operator_federation_exchange_failed" } };
    answerApi(() => Response.json(failure, { status: 502 }));
    const container = await renderLive(<ActionRequests />, [request("decision_pending", 1)]);
    await act(async () => button(container, "Approve").click());
    await vi.waitFor(() => expect(container.textContent).toContain(JSON.stringify(failure)));
    expect(container.textContent).not.toContain("[object Object]");
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
    const container = await renderLive(<ActionRequests />, [row]);

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
      const container = await renderLive(<ActionRequests />, [row]);
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
    const container = await renderLive(<ActionRequests />, [request("decision_pending", 1)]);
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
    const container = await renderLive(<ActionRequests />, [sshExec("decision_pending")]);
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
    const container = await renderLive(<ActionRequests />, [row]);

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
    const container = await renderLive(<ActionRequests />, [request("decision_pending", 1), unfit]);
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
    expect(container.textContent).toContain('"test_extra": "test-value"');
  });

  it("renders the caller's own title and description verbatim, as plain text", async () => {
    const row = {
      ...request("decision_pending", 1),
      title: "delete the <b>crashlooping</b> test pod",
      description: "Restarted 14 times in *5* minutes; the rest of the test deployment is healthy.",
    };
    const container = await renderLive(<ActionRequests />, [row]);
    expect(container.textContent).toContain(row.title);
    expect(container.textContent).toContain(row.description);
    expect(container.innerHTML).not.toContain("<b>");
    expect(container.querySelector("em")).toBeNull();
  });

  it("renders a request whose caller supplied no description", async () => {
    const row = { ...request("decision_pending", 1), description: null };
    const container = await renderLive(<ActionRequests />, [row]);
    expect(container.textContent).toContain(row.title);
    expect(container.textContent).not.toContain("null");
  });

  it.each([
    ["Approve", "allow", "allowed"],
    ["Deny", "deny", "denied"],
  ] as const)("sends a human %s decision and replaces the pending receipt", async (label, verdict, state) => {
    const pending = request("decision_pending", 1);
    const sent = answerApi(() => ({ ...pending, state, version: 2 }));
    const container = await renderLive(<ActionRequests />, [pending]);

    await act(async () => button(container, label).click());

    expect(sent).toMatchObject([
      { method: "POST", path: `/actions/${pending.id}/decision`, body: { verdict, expected_version: 1 } },
    ]);
    expect(container.textContent).toContain("Pending (0)");
  });

  it("renders server-pushed pending state without requests of its own and closes the stream", async () => {
    const sent = answerApi(() => undefined);
    vi.stubGlobal("EventSource", TestEventSource);
    const container = await mount(<ActionRequests />);
    expect(container.textContent).toContain("Loading actions");
    expect(container.textContent).not.toContain("No requests are waiting");
    expect(container.textContent).not.toContain("Pending (0)");
    await send("not JSON");
    expect(container.textContent).toContain("The live Action update was invalid");
    await send([]);
    expect(container.textContent).toContain("No requests are waiting");
    expect(container.textContent).not.toContain("Loading actions");
    await send([request("decision_pending", 1)]);
    expect(container.textContent).toContain("Pending (1)");
    await send([request("denied", 1)]);
    expect(container.textContent).toContain("Pending (0)");
    expect(sent).toEqual([]);
    const stream = TestEventSource.current;
    expect(stream?.url).toBe("/actions/stream?state=decision_pending");
    await unmountLast();
    expect(stream?.close).toHaveBeenCalledOnce();
  });

  it("keeps a dropped stream's requests without comment until it has been down a minute", async () => {
    vi.useFakeTimers();
    try {
      const container = await renderLive(<ActionRequests />, [request("decision_pending", 1)]);
      await act(async () => {
        // A drop is the network's, which the browser retries: the source stays CONNECTING.
        TestEventSource.current!.readyState = 0;
        TestEventSource.current?.dispatchEvent(new Event("error"));
      });
      expect([...streamRegistry.getStatuses().values()].map((status) => status.standing)).toEqual(["current"]);
      expect(container.textContent).toContain("Pending (1)");

      await act(async () => vi.advanceTimersByTime(STALE_AFTER_MS));
      expect([...streamRegistry.getStatuses().values()].map((status) => status.standing)).toEqual(["stale"]);
      expect(container.textContent).toContain("Pending (1)");
    } finally {
      vi.useRealTimers();
    }
  });
});
