// @vitest-environment happy-dom

import { act, type JSX } from "react";
import { MemoryRouter, useLocation } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { STALE_AFTER_MS } from "../stream_status";
import { TopbarContext } from "../topbar";
import { actionService, type ActionRequestView, type ActionService } from "./client";
import { ActionRequests, stateLabel } from "./requests";
import { ActionAffordance, ComposerPendingActions } from "./affordance";
import { button, mount, render, request, SSH_EXEC_ARGUMENTS, sshExec, unmountLast } from "./testing";

function CurrentRoute(): JSX.Element {
  const location = useLocation();
  return <div data-current-path={location.pathname} />;
}

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
    const details = container.querySelector("details");
    const summary = details?.querySelector("summary");
    if (!details || !summary) throw new Error("missing grant audit disclosure");
    expect(details.open).toBe(false);
    await act(async () => summary.click());
    expect(details.open).toBe(true);
    expect(details.textContent).toContain(grant.grant_id);
    expect(details.textContent).toContain("Revision 7");
    expect(details.textContent).toContain("Historical submission evidence");
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
      const details = container.querySelector("details");
      expect(details?.textContent).not.toContain("Authenticated external caller");
      expect(details?.querySelector("summary")?.textContent).toBe("Request audit details");
      expect(details?.open).toBe(false);
      expect(details?.textContent).toContain(row.id);
      expect(button(container, "Approve").disabled).toBe(false);
    }
  );

  it("renders every pending request with its unredacted arguments", async () => {
    const service: ActionService = { list: vi.fn(async () => [request("decision_pending", 1)]), decide: vi.fn() };
    const container = await render(service, ActionRequests);
    expect(container.textContent).not.toContain(stateLabel("decision_pending"));
    expect(button(container, "Approve").getAttribute("aria-label")).toBe("Approve");
    expect(button(container, "Deny").getAttribute("aria-label")).toBe("Deny");
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

    expect(decide).toHaveBeenCalledOnce();
    expect(decide).toHaveBeenCalledWith(expect.objectContaining({ state: "decision_pending" }), verdict);
    expect(container.textContent).not.toContain("Pending (1)");
  });

  it("renders server-pushed pending state without list polling and closes the stream", async () => {
    let stream: EventTarget | undefined;
    const close = vi.fn();
    class Stream extends EventTarget {
      onerror = null;
      close = close;
      constructor(url: string) {
        super();
        expect(url).toBe("/actions/stream?state=decision_pending");
        stream = this;
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
        stream?.dispatchEvent(new MessageEvent("snapshot", { data: "not JSON" }));
      });
      expect(container.textContent).toContain("The live Action update was invalid");
      await act(async () => {
        stream?.dispatchEvent(new MessageEvent("snapshot", { data: "[]" }));
      });
      expect(container.textContent).toContain("No requests are waiting");
      expect(container.textContent).not.toContain("Loading actions");
      await act(async () => {
        stream?.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify([request("decision_pending", 1)]) }));
      });
      expect(container.textContent).toContain("Pending (1)");
      await act(async () => {
        stream?.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify([request("denied", 1)]) }));
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
    vi.useFakeTimers({ now: new Date(2026, 0, 1, 17, 21, 4) });
    let stream: EventTarget | undefined;
    class Stream extends EventTarget {
      // A drop is the network's, which the browser retries: the source stays CONNECTING.
      readyState = 0;
      close = vi.fn();
      constructor() {
        super();
        stream = this;
      }
    }
    vi.stubGlobal("EventSource", Stream);
    try {
      const container = await render(actionService, ActionRequests);
      await act(async () => {
        stream?.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify([request("decision_pending", 1)]) }));
        stream?.dispatchEvent(new Event("error"));
      });
      await act(async () => vi.advanceTimersByTime(STALE_AFTER_MS - 1));
      expect(container.querySelector('[role="alert"]')).toBeNull();
      expect(container.textContent).toContain("Pending (1)");

      await act(async () => vi.advanceTimersByTime(1));
      expect(container.querySelector('[role="alert"]')?.textContent).toBe(
        "What's on screen may be out of date; last update 17:21:04"
      );
      expect(container.textContent).toContain("Pending (1)");
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });
});

describe("global Action affordance", () => {
  it("keeps incoming approvals modeless and collapsed, and shares decisions with the top bar", async () => {
    let stream: EventTarget | undefined;
    const close = vi.fn();
    class Stream extends EventTarget {
      close = close;
      constructor(url: string) {
        super();
        expect(url).toBe("/actions/stream?state=decision_pending");
        stream = this;
      }
    }
    vi.stubGlobal("EventSource", Stream);
    const decide = vi.spyOn(actionService, "decide").mockImplementation(async (row, verdict) => ({
      ...row,
      state: verdict === "allow" ? "allowed" : "denied",
      version: row.version + 1,
    }));
    const send = async (rows: ActionRequestView[]): Promise<void> => {
      await act(async () => stream?.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(rows) })));
    };
    const topbar = document.createElement("div");
    document.body.append(topbar);
    try {
      const container = await mount(
        <MemoryRouter initialEntries={["/threads/test-thread"]}>
          <TopbarContext.Provider value={{ title: null, actions: topbar }}>
            <ActionAffordance>
              <CurrentRoute />
              <textarea aria-label="Composer" />
              <ComposerPendingActions />
            </ActionAffordance>
          </TopbarContext.Provider>
        </MemoryRouter>
      );
      const first = request("decision_pending", 1);
      const second = request("decision_pending", 2);
      const composer = container.querySelector<HTMLTextAreaElement>('textarea[aria-label="Composer"]');
      if (!composer) throw new Error("missing composer");
      composer.focus();
      await send([first]);
      expect(document.activeElement).toBe(composer);
      expect(container.querySelector(".action-affordance-notice")?.textContent).toContain(
        "1 action waiting for review"
      );
      expect(container.querySelector('.action-affordance-notice button[aria-expanded="false"]')).not.toBeNull();
      expect(container.querySelector(".action-affordance-notice .agentplane-code-block")).toBeNull();
      expect(document.querySelector('[aria-modal="true"]')).toBeNull();
      expect(document.querySelector(".mantine-Drawer-root")).toBeNull();
      expect(close).not.toHaveBeenCalled();

      const topbarButton = topbar.querySelector<HTMLButtonElement>('button[aria-label="Actions, 1 pending"]');
      if (!topbarButton) throw new Error("missing top bar Actions count");
      await act(async () => topbarButton.click());
      expect(container.querySelector("[data-current-path]")?.getAttribute("data-current-path")).toBe("/actions");

      await act(async () => button(container, "Review").click());
      expect(container.querySelector(".action-affordance-notice .agentplane-code-block")).not.toBeNull();
      expect(container.textContent).toContain("Exact arguments (unredacted)");
      await send([first, second]);
      expect(container.querySelector(".action-affordance-notice")?.textContent).toContain(second.title);
      await act(async () => button(container, "Deny").click());
      expect(decide).toHaveBeenCalledWith(first, "deny");
      await send([second]);
      expect(container.querySelector(".action-affordance-notice")?.textContent).toContain(second.title);
      await act(async () => button(container, "Approve").click());
      expect(container.querySelector(".action-affordance-notice")).toBeNull();
      await unmountLast();
      expect(close).toHaveBeenCalledOnce();
    } finally {
      topbar.remove();
      decide.mockRestore();
      vi.unstubAllGlobals();
    }
  });
});
