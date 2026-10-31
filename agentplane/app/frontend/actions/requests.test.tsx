// @vitest-environment happy-dom

import { act } from "react";
import { describe, expect, it, vi } from "vitest";

import { STALE_AFTER_MS, streamRegistry } from "../stream_status";
import { ActionRequests, stateLabel } from "./requests";
import {
  ACTION_REQUEST_WATCH,
  TestNdjsonWatch,
  answerApi,
  button,
  mount,
  renderLive,
  request,
  send,
  sshExec,
  unmountLast,
} from "./testing/harness";

describe("ActionRequests", () => {
  it("shows a refused decision's structured error", async () => {
    const failure = { detail: { code: "operator_federation_exchange_failed" } };
    answerApi(() => Response.json(failure, { status: 502 }));
    const container = await renderLive(<ActionRequests />, [request("decision_pending", 1)]);
    await act(async () => button(container, "Approve").click());
    await vi.waitFor(() => expect(container.textContent).toContain(JSON.stringify(failure)));
    expect(container.textContent).not.toContain("[object Object]");
  });

  it("summarizes who asked and when, and folds the full caller and request id behind it", async () => {
    const row = request("decision_pending", 1);
    const container = await renderLive(<ActionRequests />, [row]);
    const control = button(container, row.caller.name);
    expect(control.getAttribute("aria-expanded")).toBe("false");
    const requested = control.querySelector("time");
    if (!requested) throw new Error("missing the request time in the audit summary");
    expect(Date.parse(requested.dateTime)).toBe(Date.parse(row.created_at));
    expect(container.textContent).toContain(`Requested by ${row.caller.namespace}/${row.caller.name}`);
    expect(container.textContent).toContain(row.id);
    expect(button(container, "Approve").disabled).toBe(false);
  });

  it("renders every pending request with its arguments", async () => {
    const container = await renderLive(<ActionRequests />, [request("decision_pending", 1)]);
    expect(container.textContent).not.toContain(stateLabel("decision_pending"));
    expect(button(container, "Approve").getAttribute("aria-label")).toBe("Approve");
    expect(button(container, "Deny").getAttribute("aria-label")).toBe("Deny");
    expect(button(container, "Approve").querySelector("svg")).not.toBeNull();
    expect(button(container, "Deny").querySelector("svg")).not.toBeNull();
    expect(container.textContent).toContain("Exact arguments");
    expect(container.textContent).toContain("test-exact-token");
    expect(container.textContent).toContain("test-exact-password");
  });

  it("draws a registered Action's arguments with its widget, and the card's Raw switch turns them to JSON", async () => {
    const container = await renderLive(<ActionRequests />, [sshExec("decision_pending")]);
    expect(container.textContent).toContain("test-user@test-host.example");
    expect(container.querySelector(".agentplane-command-call")).not.toBeNull();
    expect(container.textContent).not.toContain('"command"');
    const raw = container.querySelector<HTMLInputElement>('input[type="checkbox"]');
    if (!raw) throw new Error("missing the Raw switch");
    await act(async () => raw.click());
    expect(container.textContent).toContain('"command": "echo test-output"');
    expect(container.querySelector(".agentplane-command-call")).toBeNull();
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
    // An unregistered Action has no pretty argument view for Raw to switch back from.
    const container = await renderLive(<ActionRequests />, [request("decision_pending", 1)]);
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
    expect(container.textContent).toContain('"token": "test-exact-token"');
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
      {
        method: "POST",
        path: `/upstream/actions/v1/operator/action-requests/${pending.id}/decision`,
        body: { verdict: `VERDICT_${verdict.toUpperCase()}`, expectedVersion: "1" },
      },
    ]);
    expect(container.textContent).toContain("Pending (0)");
  });

  it("renders server-pushed pending state without requests of its own and closes the stream", async () => {
    const sent = answerApi(() => undefined);
    const container = await mount(<ActionRequests />);
    expect(container.textContent).toContain("Loading actions");
    expect(container.textContent).not.toContain("No requests are waiting");
    expect(container.textContent).not.toContain("Pending (0)");
    await send([]);
    expect(container.textContent).toContain("No requests are waiting");
    expect(container.textContent).not.toContain("Loading actions");
    await send([request("decision_pending", 1)]);
    expect(container.textContent).toContain("Pending (1)");
    await send([request("denied", 1)]);
    expect(container.textContent).toContain("Pending (0)");
    expect(sent).toEqual([]);
    const stream = TestNdjsonWatch.opened.get(ACTION_REQUEST_WATCH);
    expect(stream?.url).toBe(ACTION_REQUEST_WATCH);
    await unmountLast();
    expect(stream?.signal?.aborted).toBe(true);
  });

  it("keeps a dropped stream's requests without comment until it has been down a minute", async () => {
    vi.useFakeTimers();
    try {
      const container = await renderLive(<ActionRequests />, [request("decision_pending", 1)]);
      await act(async () => {
        // A dropped fetch stream is retried with a fresh request after backoff.
        TestNdjsonWatch.current!.drop();
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
