// @vitest-environment happy-dom

import { act } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ActionGroupView, ActionRequestView, ActionState } from "./client";
import { ActionHistory } from "./history";
import { stateLabel } from "./requests";
import {
  TestEventSource,
  answerApi,
  button,
  DECISION_NOTE,
  renderLive,
  request,
  send,
  sshExec,
  unmountLast,
} from "./testing";

/** The history view over one page of `rows`, reading `groups` (a failure, when a `Response`) as the
 * Action groups; resolves once the page has loaded. */
async function renderHistory(
  rows: ActionRequestView[] | Response,
  groups: ActionGroupView[] | Response = []
): Promise<HTMLDivElement> {
  // The page waits for the groups, so they have arrived by the time it shows.
  let resolve = (): void => {};
  const groupsAnswered = new Promise<void>((done) => (resolve = done));
  answerApi(async ({ path }) => {
    if (path === "/action-groups") {
      resolve();
      return groups instanceof Response ? groups.clone() : groups;
    }
    if (path !== "/actions/history") return undefined;
    await groupsAnswered;
    return rows instanceof Response ? rows.clone() : { items: rows, next_cursor: null };
  });
  const container = await renderLive(<ActionHistory />);
  await vi.waitFor(() => expect(container.textContent).not.toContain("Loading actions"));
  return container;
}

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

function gmailDraft(): ActionRequestView {
  const row = request("succeeded", 1);
  return {
    ...row,
    action: { group: "gmail", name: "drafts_create" },
    arguments: {
      to: ["reader@example.com"],
      cc: ["copy@example.com"],
      subject: "Release notes",
      body: "The release is ready.\nPlease review it.",
    },
    title: "Prepare the release email",
    execution: {
      ...row.execution!,
      result: {
        content: [],
        structuredContent: {
          id: "draft-123",
          message: { payload: { headers: [{ name: "Subject", value: "Release notes saved" }] } },
        },
        isError: false,
      },
    },
  };
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
    const container = await renderHistory(Response.json(failure, { status: 502 }));
    expect(container.textContent).toContain(JSON.stringify(failure));
    expect(container.textContent).not.toContain("[object Object]");
    expect(container.textContent).not.toContain("No decided requests");
    expect(container.textContent).not.toContain("Loading actions");
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
    const container = await renderHistory(states.map(request));

    for (const state of states) expect(container.textContent).toContain(stateLabel(state));
    expect(container.textContent).toContain("requested by agentplane-test/test-agent");
    expect(container.textContent).toContain("Allowed");
    expect(container.textContent).toContain("Denied");
    expect(container.textContent).toContain(DECISION_NOTE);
    // The caller's own framing stays on the receipt, beside the operator's decision note.
    for (const state of states) {
      expect(container.textContent).toContain(`test title for the ${state} fixture`);
      expect(container.textContent).toContain(`test description adding what the ${state} title leaves out`);
    }
    expect(container.textContent).toContain("Result");
    expect(container.textContent).toContain("Execution error");
  });

  it("places the execution state beneath its result", async () => {
    const container = await renderHistory([succeeded("test_sandbox", { ok: true })]);
    const outcome = container.querySelector<HTMLElement>('[data-testid="action-execution-outcome"]');
    const state = outcome?.querySelector('[data-testid="action-execution-state"]');

    expect(outcome?.firstElementChild?.textContent).toContain("Result");
    expect(state?.textContent).toBe("succeeded");
    expect(outcome?.lastElementChild).toBe(state);
  });

  it("shows the exact arguments open, as the pending card does", async () => {
    const container = await renderHistory([request("succeeded", 1)]);
    expect(container.textContent).toContain("Exact arguments (unredacted)");
    const block = [...container.querySelectorAll(".agentplane-code-block")].find((node) =>
      node.textContent?.includes("test-exact-token")
    );
    if (!block) throw new Error("missing the arguments");
    expect(block.closest(".agentplane-disclosure-panel")).toBeNull();
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
            source: "rules",
            index: 0,
            type: "exact_actions",
          },
        },
      },
    };
    const container = await renderHistory([row]);
    expect(container.textContent).toContain("Auto-approved via policy");
    expect(container.textContent).toContain("fixture-auto-allow");
  });

  it("loads a bounded history page, follows invalidations, and pages on demand", async () => {
    const sent = answerApi(({ path, query }) => {
      if (path === "/action-groups") return [];
      if (path !== "/actions/history") return undefined;
      return query.get("cursor") === "page2"
        ? { items: [request("succeeded", 2)], next_cursor: null }
        : { items: [request("denied", 1)], next_cursor: "page2" };
    });
    const container = await renderLive(<ActionHistory />);
    await vi.waitFor(() => expect(container.textContent).toContain("Denied"));
    await send([]);
    await vi.waitFor(() => expect(button(container, "Load more")).toBeDefined());
    await act(async () => button(container, "Load more").click());
    await vi.waitFor(() => expect(container.textContent).toContain("succeeded"));
    // The first page on mount and again on each of the stream's two snapshots, then the next page on demand.
    expect(sent.filter(({ path }) => path === "/actions/history").map(({ query }) => query.get("cursor"))).toEqual([
      null,
      null,
      null,
      "page2",
    ]);
    const stream = TestEventSource.current;
    expect(stream?.url).toBe("/actions/stream?state=decision_pending");
    await unmountLast();
    expect(stream?.close).toHaveBeenCalledOnce();
  });

  it("draws an MCP group's result the way the tool answered it", async () => {
    const container = await renderHistory([succeeded("test_mcp", IMAGE_RESULT)], [group("test_mcp", "mcp")]);
    expect(container.querySelector("img")?.getAttribute("src")).toBe(`data:image/png;base64,${IMAGE_DATA}`);
    expect(container.textContent).toContain("test caption");
    expect(container.textContent).not.toContain('"content"');
  });

  it("switches an MCP result between the tool's answer and its stored JSON", async () => {
    const container = await renderHistory([succeeded("test_mcp", IMAGE_RESULT)], [group("test_mcp", "mcp")]);
    const raw = container.querySelector<HTMLInputElement>('input[type="checkbox"]');
    expect(raw).not.toBeNull();
    await act(async () => raw!.click());
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain('"mimeType": "image/png"');
  });

  it("turns an ssh exec call's request and response to their JSON together, and back", async () => {
    const container = await renderHistory([sshExec("succeeded")], [group("ssh", "mcp")]);
    // The request widget shows the command alone in its block, the response widget the exit code.
    const drawn = (): { request: boolean; response: boolean } => ({
      request: [...container.querySelectorAll(".agentplane-code-block")].some(
        (block) =>
          [...block.querySelectorAll(".cm-line")]
            .map((line) => line.textContent ?? "")
            .join("\n")
            .trim() === "echo test-output"
      ),
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

  it("shows a Gmail draft's arguments and result together, with both available in Raw", async () => {
    const container = await renderHistory([gmailDraft()], [group("gmail", "mcp")]);
    expect(container.textContent).toContain("Call");
    expect(container.textContent).toContain("Release notes saved");
    expect(container.textContent).toContain("reader@example.com");
    expect(container.textContent).toContain("The release is ready.");
    expect(container.textContent).not.toContain("Exact arguments (unredacted)");
    expect(container.querySelector('[data-testid="action-execution-outcome"]')?.textContent).not.toContain("Result");
    expect(
      container.querySelector('a[href="https://mail.google.com/mail/u/0/#drafts?compose=draft-123"]')
    ).not.toBeNull();

    const raw = container.querySelector<HTMLInputElement>('input[type="checkbox"]');
    expect(raw).not.toBeNull();
    await act(async () => raw!.click());
    expect(container.textContent).toContain("Exact arguments (unredacted)");
    expect(container.textContent).toContain('"subject": "Release notes"');
    expect(container.textContent).toContain('"id": "draft-123"');
    expect(container.querySelector('[data-testid="action-execution-outcome"]')?.textContent).toContain("Result");
  });

  it("keeps a sandbox group's result as its stored JSON, without a Raw switch, even one shaped like a CallToolResult", async () => {
    const container = await renderHistory(
      [succeeded("test_sandbox", IMAGE_RESULT)],
      [group("test_sandbox", "sandbox")]
    );
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain('"mimeType": "image/png"');
  });

  it("shows results as their stored JSON, saying why, when the Action groups cannot be read", async () => {
    const container = await renderHistory(
      [succeeded("test_mcp", IMAGE_RESULT)],
      Response.json({ detail: "test group listing outage" }, { status: 503 })
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain('"mimeType": "image/png"');
    expect(container.textContent).toContain("test group listing outage");
  });
});
