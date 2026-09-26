// @vitest-environment happy-dom

import { act } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  actionService,
  type ActionGroupService,
  type ActionGroupView,
  type ActionRequestView,
  type ActionService,
  type ActionState,
} from "./client";
import { ActionHistory } from "./history";
import { stateLabel } from "./requests";
import { render, request, sshExec, unmountLast, type View } from "./testing";

/** The view reading its Action groups from `list` rather than the real `/action-groups`. */
function historyOver(list: ActionGroupService["list"]): View {
  const groupService: ActionGroupService = { list };
  return (props) => <ActionHistory {...props} groupService={groupService} />;
}

const withoutGroups = historyOver(async () => []);

function group(key: string, executorKind: string): ActionGroupView {
  return {
    key,
    title: key,
    description: `test group ${key}`,
    executor_kind: executorKind,
    executor_description: "test executor",
    available: true,
    actions: [],
  };
}

function succeeded(groupKey: string, result: unknown): ActionRequestView {
  const row = request("succeeded", 1);
  return { ...row, action: { group: groupKey, name: "test_tool" }, execution: { ...row.execution!, result } };
}

// base64 of "test-image-bytes": nothing here decodes it.
const IMAGE_DATA = "dGVzdC1pbWFnZS1ieXRlcw==";
const IMAGE_RESULT = {
  content: [
    { type: "text", text: "test caption" },
    { type: "image", data: IMAGE_DATA, mimeType: "image/png" },
  ],
  isError: false,
};

describe("ActionHistory", () => {
  it("shows structured list errors without an empty-state claim", async () => {
    const failure = { detail: { code: "operator_federation_exchange_failed" } };
    const container = await render({ list: vi.fn().mockRejectedValue(failure), decide: vi.fn() }, withoutGroups);
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
      caller: { namespace: "agentplane-test", name: "test-caller" },
      external_grant: grant,
    };
    const container = await render({ list: async () => [row], decide: vi.fn() }, withoutGroups);

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

    const container = await render(service, withoutGroups);

    for (const state of states) expect(container.textContent).toContain(stateLabel(state));
    expect(container.textContent).toContain("requested by agentplane-test/test-agent");
    expect(container.textContent).toContain("Allowed");
    expect(container.textContent).toContain("Denied");
    expect(container.textContent).toContain("Reviewed scope — allowed for this request.");
    // The caller's own framing stays on the receipt, beside the operator's decision note.
    for (const state of states) {
      expect(container.textContent).toContain(`test title for the ${state} fixture`);
      expect(container.textContent).toContain(`test description adding what the ${state} title leaves out`);
    }
    expect(container.textContent).toContain("Result");
    expect(container.textContent).toContain("Execution error");
  });

  it("shows the exact arguments open, as the pending card does", async () => {
    const container = await render({ list: async () => [request("succeeded", 1)], decide: vi.fn() }, withoutGroups);
    expect(container.textContent).toContain("Exact arguments (unredacted)");
    const block = [...container.querySelectorAll("pre")].find((pre) => pre.textContent?.includes("test-exact-token"));
    if (!block) throw new Error("missing the arguments");
    expect(block.closest("details, [aria-hidden='true'], [hidden]")).toBeNull();
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
    const container = await render({ list: async () => [row], decide: vi.fn() }, withoutGroups);
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
      const container = await render(actionService, withoutGroups);
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

  it("draws an MCP group's result the way the tool answered it", async () => {
    const container = await render(
      { list: async () => [succeeded("test_mcp", IMAGE_RESULT)], decide: vi.fn() },
      historyOver(async () => [group("test_mcp", "mcp")])
    );
    expect(container.querySelector("img")?.getAttribute("src")).toBe(`data:image/png;base64,${IMAGE_DATA}`);
    expect(container.textContent).toContain("test caption");
    expect(container.textContent).not.toContain('"content"');
  });

  it("switches an MCP result between the tool's answer and its stored JSON", async () => {
    const container = await render(
      { list: async () => [succeeded("test_mcp", IMAGE_RESULT)], decide: vi.fn() },
      historyOver(async () => [group("test_mcp", "mcp")])
    );
    const raw = container.querySelector<HTMLInputElement>('input[type="checkbox"]');
    expect(raw).not.toBeNull();
    await act(async () => raw!.click());
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain('"mimeType": "image/png"');
  });

  it("turns an ssh exec call's request and response to their JSON together, and back", async () => {
    const container = await render(
      { list: async () => [sshExec("succeeded")], decide: vi.fn() },
      historyOver(async () => [group("ssh", "mcp")])
    );
    // The request widget shows the command alone in its block, the response widget the exit code.
    const drawn = (): { request: boolean; response: boolean } => ({
      request: [...container.querySelectorAll("pre")].some((block) => block.textContent === "echo test-output"),
      response: container.textContent?.includes("Exit 0") ?? false,
    });
    const switches = container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]');
    expect(switches).toHaveLength(1);
    expect(drawn()).toEqual({ request: true, response: true });
    await act(async () => switches[0].click());
    expect(drawn()).toEqual({ request: false, response: false });
    expect(container.textContent).toContain('"command": "echo test-output"');
    expect(container.textContent).toContain('"structuredContent"');
    await act(async () => switches[0].click());
    expect(drawn()).toEqual({ request: true, response: true });
  });

  it("offers no Raw switch where the stored JSON is the only rendering", async () => {
    const container = await render(
      { list: async () => [succeeded("test_sandbox", IMAGE_RESULT)], decide: vi.fn() },
      historyOver(async () => [group("test_sandbox", "sandbox")])
    );
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
  });

  it("keeps a sandbox group's result as its stored JSON, even one shaped like a CallToolResult", async () => {
    const container = await render(
      { list: async () => [succeeded("test_sandbox", IMAGE_RESULT)], decide: vi.fn() },
      historyOver(async () => [group("test_sandbox", "sandbox")])
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain('"mimeType": "image/png"');
  });

  it("shows results as their stored JSON, saying why, when the Action groups cannot be read", async () => {
    const container = await render(
      { list: async () => [succeeded("test_mcp", IMAGE_RESULT)], decide: vi.fn() },
      historyOver(vi.fn().mockRejectedValue(new Error("test group listing outage")))
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain('"mimeType": "image/png"');
    expect(container.textContent).toContain("test group listing outage");
  });
});
