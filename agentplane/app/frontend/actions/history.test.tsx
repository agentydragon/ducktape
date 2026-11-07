// @vitest-environment happy-dom

import { act } from "react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { clone, create, fromJson, type JsonValue } from "@bufbuild/protobuf";
import { TimestampSchema, ValueSchema } from "@bufbuild/protobuf/wkt";
import {
  ActionGroupSchema,
  ActionIdentitySchema,
  ActionRequestSchema,
  ActionState,
  DecisionSchema,
  ExecutorKind,
  ListActionRequestHistoryResponseSchema,
  MatchedPolicySchema,
  PolicyEvidenceSchema,
  PolicyKind,
  PolicySource,
  type ListActionRequestHistoryResponse,
  type ActionGroup,
  type ActionRequest,
} from "../../../action_service/action_service_pb";
import { TestIntersectionObserver } from "../../../../util/frontend/test_intersection_observer.js";
import { ActionHistory } from "./history";
import { stateLabel } from "./requests";
import {
  ACTION_REQUEST_WATCH,
  TestNdjsonWatch,
  answerApi,
  button,
  DECISION_NOTE,
  renderLive,
  request,
  send,
  sshExec,
  unmountLast,
} from "./testing/harness";

async function showNextPage(container: HTMLElement): Promise<void> {
  const sentinel = container.querySelector('[data-testid="action-history-sentinel"]');
  if (!sentinel) throw new Error("missing history sentinel");
  await act(async () => TestIntersectionObserver.intersect(sentinel, true));
}

/** The history view over one page of `rows`, reading `groups` (a failure, when a `Response`) as the
 * Action groups; resolves once the page has loaded. */
async function renderHistory(
  rows: ActionRequest[] | Response,
  groups: ActionGroup[] | Response = []
): Promise<HTMLDivElement> {
  // The page waits for the groups, so they have arrived by the time it shows.
  let resolve = (): void => {};
  const groupsAnswered = new Promise<void>((done) => (resolve = done));
  answerApi(async ({ path }) => {
    if (path === "/upstream/actions/v1/action-groups") {
      resolve();
      return groups instanceof Response ? groups.clone() : groups;
    }
    if (path !== "/upstream/actions/v1/action-requests:history") return undefined;
    await groupsAnswered;
    return rows instanceof Response ? rows.clone() : historyPage(rows);
  });
  const container = await renderLive(
    <MemoryRouter>
      <ActionHistory />
    </MemoryRouter>
  );
  await vi.waitFor(() => expect(container.textContent).not.toContain("Loading actions"));
  return container;
}

function historyPage(actionRequests: ActionRequest[], nextCursor?: string): ListActionRequestHistoryResponse {
  return create(ListActionRequestHistoryResponseSchema, { actionRequests, nextCursor });
}

function group(key: string, executorKind: ExecutorKind): ActionGroup {
  return create(ActionGroupSchema, {
    key,
    title: key,
    description: `test group ${key}`,
    executorKind,
    executorDescription: "test executor",
    available: true,
    actions: [],
    directTools: [],
  });
}

function succeeded(groupKey: string, result: JsonValue): ActionRequest {
  const row = request(ActionState.SUCCEEDED, 1);
  row.action = create(ActionIdentitySchema, { group: groupKey, name: "test_tool" });
  row.execution!.result = fromJson(ValueSchema, result);
  return row;
}

function withDecisionTime(row: ActionRequest, value: string): ActionRequest {
  const copy = clone(ActionRequestSchema, row);
  if (copy.decision === undefined) throw new Error("Fixture has no Decision.");
  copy.decision.decidedAt = fromJson(TimestampSchema, value);
  return copy;
}

function stateName(state: ActionState): string {
  return ActionState[state].replace("ACTION_STATE_", "").toLowerCase();
}

function gmailDraft(): ActionRequest {
  const row = request(ActionState.SUCCEEDED, 1);
  row.action = create(ActionIdentitySchema, { group: "gmail", name: "drafts_create" });
  row.arguments = {
    to: ["reader@example.com"],
    cc: ["copy@example.com"],
    subject: "Release notes",
    body: "The release is ready.\nPlease review it.",
  };
  row.title = "Prepare the release email";
  row.execution!.result = fromJson(ValueSchema, {
    content: [],
    structuredContent: {
      id: "draft-123",
      message: { payload: { headers: [{ name: "Subject", value: "Release notes saved" }] } },
    },
    isError: false,
  });
  return row;
}

// base64 of "test-image-bytes": nothing here decodes it.
const IMAGE_DATA = "dGVzdC1pbWFnZS1ieXRlcw==";
const IMAGE_RESULT: JsonValue = {
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
      ActionState.ALLOWED,
      ActionState.DENIED,
      ActionState.DISPATCHING,
      ActionState.RUNNING,
      ActionState.SUCCEEDED,
      ActionState.FAILED,
      ActionState.CANCELLED,
      ActionState.EXECUTION_UNKNOWN,
    ];
    const container = await renderHistory(states.map(request));

    for (const state of states) expect(container.textContent).toContain(stateLabel(state));
    expect(container.textContent).toContain("Requested by agentplane-test/test-agent");
    expect(container.textContent).toContain("Allowed");
    expect(container.textContent).toContain("Denied");
    expect(container.textContent).toContain(DECISION_NOTE);
    // The caller's own framing stays on the receipt, beside the operator's decision note.
    for (const state of states) {
      expect(container.textContent).toContain(`test title for the ${stateName(state)} fixture`);
      expect(container.textContent).toContain(`test description adding what the ${stateName(state)} title leaves out`);
    }
    expect(container.textContent).toContain("Result");
    expect(container.textContent).toContain("Execution error");
  });

  it("shows one status, with the operator's decision and wait in the timestamp line", async () => {
    const row = withDecisionTime(succeeded("test_sandbox", { ok: true }), "2026-09-05T12:00:56Z");
    const container = await renderHistory([row]);
    const state = container.querySelector('[data-testid="action-execution-state"]');
    expect(state?.textContent).toBe("succeeded");
    expect(state?.closest('[data-testid="action-execution-outcome"]')).toBeNull();
    expect(container.querySelector("time")?.parentElement?.textContent).toContain(
      "Approved after 56 seconds · Completed"
    );
    expect(container.textContent).not.toContain("Allowed");
    const summary = [...container.querySelectorAll("button")].find((node) => node.textContent === "Receipt details");
    expect(summary?.getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelector('a[href^="/actions/"]')).not.toBeNull();
    expect(container.querySelector("time")?.getAttribute("datetime")).toBe("2026-09-05T12:00:02.000Z");
  });

  it("opens failed receipts and makes tool errors visible even when dispatch succeeded", async () => {
    const row = succeeded("test_mcp", { content: [{ type: "text", text: "permission rejected" }], isError: true });
    const container = await renderHistory([row], [group("test_mcp", ExecutorKind.MCP)]);
    expect(container.querySelector('[data-testid="action-execution-state"]')?.textContent).toBe("Tool error");
    const summary = [...container.querySelectorAll("button")].find((node) => node.textContent === "Receipt details");
    expect(summary?.getAttribute("aria-expanded")).toBe("true");
  });

  it("shows a command failure in the summary while retaining its decision", async () => {
    const container = await renderHistory(
      [sshExec(ActionState.SUCCEEDED, { exit_code: 7 })],
      [group("ssh", ExecutorKind.MCP)]
    );
    expect(container.querySelector('[data-testid="action-execution-state"]')?.textContent).toBe("Command exited 7");
    expect(container.textContent).toContain("Approved · Completed");
  });

  it("names a request's decision once while it has not run", async () => {
    const row = withDecisionTime(request(ActionState.DENIED, 1), "2026-09-05T12:02:00Z");
    const container = await renderHistory([row]);
    expect(container.querySelector('[data-testid="action-execution-state"]')?.textContent).toBe("Denied");
    expect(container.textContent).toContain("Decided after 2 minutes · Updated");
  });

  it("retains the exact arguments inside the receipt disclosure", async () => {
    const container = await renderHistory([request(ActionState.SUCCEEDED, 1)]);
    expect(container.textContent).toContain("Exact arguments");
    const block = [...container.querySelectorAll(".ui-code-block")].find((node) =>
      node.textContent?.includes("test-exact-token")
    );
    if (!block) throw new Error("missing the arguments");
    expect(block.closest("[data-clamped]")?.getAttribute("data-clamped")).toBe("false");
    expect(
      [...container.querySelectorAll("button")]
        .find((node) => node.textContent === "Receipt details")
        ?.getAttribute("aria-expanded")
    ).toBe("false");
  });

  it("renders the auto-approving policy when the Decision carries policy evidence", async () => {
    const base = request(ActionState.SUCCEEDED, 1);
    const row = clone(ActionRequestSchema, base);
    row.decision = create(DecisionSchema, {
      ...base.decision!,
      provider: "action_policy_set",
      operator: undefined,
      decisionNote: undefined,
      policyEvidence: create(PolicyEvidenceSchema, {
        bindings: [{ namespace: "agentplane-visual", name: "demo-a1b2-github-public", resourceVersion: "12345" }],
        policySets: [{ namespace: "agentplane-visual", name: "fixture-auto-allow", generation: 1n }],
        matched: create(MatchedPolicySchema, {
          namespace: "agentplane-visual",
          policySet: "fixture-auto-allow",
          source: PolicySource.RULES,
          index: 0,
          type: PolicyKind.EXACT_ACTIONS,
        }),
      }),
    });
    const container = await renderHistory([row]);
    expect(container.textContent).toContain("Auto-approved via policy fixture-auto-allow · Completed");
  });

  it("loads a bounded history page, follows invalidations, and pages on demand", async () => {
    const sent = answerApi(({ path, query }) => {
      if (path === "/upstream/actions/v1/action-groups") return [];
      if (path !== "/upstream/actions/v1/action-requests:history") return undefined;
      return query.get("cursor") === "page2"
        ? historyPage([request(ActionState.SUCCEEDED, 2)])
        : historyPage([request(ActionState.DENIED, 1)], "page2");
    });
    const container = await renderLive(
      <MemoryRouter>
        <ActionHistory />
      </MemoryRouter>
    );
    await vi.waitFor(() => expect(container.textContent).toContain("Denied"));
    await send([]);
    expect(container.querySelector('[data-testid="action-history-sentinel"]')).not.toBeNull();
    await showNextPage(container);
    await vi.waitFor(() => expect(container.textContent).toContain("succeeded"));
    expect(sent.filter(({ query }) => query.get("cursor") === "page2")).toHaveLength(1);
    expect(container.textContent).toContain("End of action history.");
    const stream = TestNdjsonWatch.opened.get(ACTION_REQUEST_WATCH);
    expect(stream?.url).toBe(ACTION_REQUEST_WATCH);
    await unmountLast();
    expect(stream?.signal?.aborted).toBe(true);
  });

  it("defaults to excluding auto-allows and resets pagination when the filter changes", async () => {
    const sent = answerApi(({ path, query }) => {
      if (path === "/upstream/actions/v1/action-groups") return [];
      if (path !== "/upstream/actions/v1/action-requests:history") return undefined;
      const all = query.get("includeAutoAllowed") === "true";
      return historyPage(
        [
          request(
            all ? ActionState.SUCCEEDED : query.has("cursor") ? ActionState.CANCELLED : ActionState.DENIED,
            all ? 3 : query.has("cursor") ? 2 : 1
          ),
        ],
        query.has("cursor") ? undefined : "page2"
      );
    });
    const container = await renderLive(
      <MemoryRouter>
        <ActionHistory />
      </MemoryRouter>
    );
    await vi.waitFor(() => expect(container.textContent).toContain("Denied"));
    expect(
      sent
        .filter(({ path }) => path === "/upstream/actions/v1/action-requests:history")
        .every(({ query }) => query.get("includeAutoAllowed") === "false")
    ).toBe(true);
    await showNextPage(container);
    await vi.waitFor(() => expect(container.textContent).toContain("cancelled"));
    const all = container.querySelector<HTMLInputElement>('input[value="all"]')!;
    await act(async () => all.click());
    await vi.waitFor(() => expect(container.textContent).toContain("succeeded"));
    expect(container.textContent).not.toContain("cancelled");
    const latest = sent.filter(({ path }) => path === "/upstream/actions/v1/action-requests:history").at(-1)!;
    expect(latest.query.get("includeAutoAllowed")).toBe("true");
    expect(latest.query.get("cursor")).toBeNull();
    const filtered = container.querySelector<HTMLInputElement>('input[value="not-auto-allowed"]')!;
    await act(async () => filtered.click());
    await vi.waitFor(() => expect(container.textContent).toContain("Denied"));
    expect(container.textContent).not.toContain("succeeded");
  });

  it("refreshes an unfinished receipt beyond the newest page on invalidation", async () => {
    const older = request(ActionState.RUNNING, 2);
    const sent = answerApi(({ path, query }) => {
      if (path === "/upstream/actions/v1/action-groups") return [];
      if (path === `/upstream/actions/v1/action-requests/${older.id}`) {
        const update = clone(ActionRequestSchema, older);
        update.state = ActionState.SUCCEEDED;
        update.version = 3n;
        return update;
      }
      if (path !== "/upstream/actions/v1/action-requests:history") return undefined;
      if (query.has("cursor")) return historyPage([older]);
      return historyPage([request(ActionState.DENIED, 1)], "page2");
    });
    const container = await renderLive(
      <MemoryRouter>
        <ActionHistory />
      </MemoryRouter>
    );
    await vi.waitFor(() => expect(container.textContent).toContain("Denied"));
    await showNextPage(container);
    await vi.waitFor(() => expect(container.textContent).toContain("running"));
    await send([]);
    await vi.waitFor(() => expect(container.textContent).toContain("succeeded"));
    expect(sent.some(({ path }) => path === `/upstream/actions/v1/action-requests/${older.id}`)).toBe(true);
  });

  it("stops automatic pagination after a failure and retries the same cursor explicitly", async () => {
    let fail = true;
    const sent = answerApi(({ path, query }) => {
      if (path === "/upstream/actions/v1/action-groups") return [];
      if (path !== "/upstream/actions/v1/action-requests:history") return undefined;
      if (!query.has("cursor")) return historyPage([request(ActionState.DENIED, 1)], "page2");
      return fail
        ? Response.json({ detail: "try again" }, { status: 503 })
        : historyPage([request(ActionState.SUCCEEDED, 2)]);
    });
    const container = await renderLive(
      <MemoryRouter>
        <ActionHistory />
      </MemoryRouter>
    );
    await vi.waitFor(() => expect(container.textContent).toContain("Denied"));
    await showNextPage(container);
    await vi.waitFor(() => expect(container.textContent).toContain("Retry loading older actions"));
    expect(sent.filter(({ query }) => query.has("cursor"))).toHaveLength(1);
    fail = false;
    await act(async () => button(container, "Retry loading older actions").click());
    await vi.waitFor(() => expect(container.textContent).toContain("End of action history."));
    expect(sent.filter(({ query }) => query.get("cursor") === "page2")).toHaveLength(2);
  });

  it("restores the all-actions filter from its URL", async () => {
    const sent = answerApi(({ path }) => {
      if (path === "/upstream/actions/v1/action-groups") return [];
      if (path === "/upstream/actions/v1/action-requests:history") return historyPage([]);
      return undefined;
    });
    const container = await renderLive(
      <MemoryRouter initialEntries={["/actions?history_filter=all"]}>
        <ActionHistory />
      </MemoryRouter>
    );
    await vi.waitFor(() => expect(container.textContent).not.toContain("Loading actions"));
    expect(container.querySelector<HTMLInputElement>('input[value="all"]')?.checked).toBe(true);
    expect(
      sent
        .filter(({ path }) => path === "/upstream/actions/v1/action-requests:history")
        .every(({ query }) => query.get("includeAutoAllowed") === "true")
    ).toBe(true);
  });

  it("draws an MCP group's result the way the tool answered it", async () => {
    const container = await renderHistory([succeeded("test_mcp", IMAGE_RESULT)], [group("test_mcp", ExecutorKind.MCP)]);
    expect(container.querySelector("img")?.getAttribute("src")).toBe(`data:image/png;base64,${IMAGE_DATA}`);
    expect(container.textContent).toContain("test caption");
    expect(container.textContent).not.toContain('"content"');
  });

  it("switches an MCP result between the tool's answer and its stored JSON", async () => {
    const container = await renderHistory([succeeded("test_mcp", IMAGE_RESULT)], [group("test_mcp", ExecutorKind.MCP)]);
    const raw = container.querySelector<HTMLInputElement>('input[type="checkbox"]');
    expect(raw).not.toBeNull();
    await act(async () => raw!.click());
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain('"mimeType": "image/png"');
  });

  it("turns an ssh exec call's request and response to their JSON together, and back", async () => {
    const container = await renderHistory([sshExec(ActionState.SUCCEEDED)], [group("ssh", ExecutorKind.MCP)]);
    // The request widget shows the command alone in its block, the response widget the exit code.
    const drawn = (): { request: boolean; response: boolean } => ({
      request: [...container.querySelectorAll(".ui-code-block")].some(
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
    const container = await renderHistory([gmailDraft()], [group("gmail", ExecutorKind.MCP)]);
    expect(container.textContent).toContain("Call");
    expect(container.textContent).toContain("Release notes saved");
    expect(container.textContent).toContain("reader@example.com");
    expect(container.textContent).toContain("The release is ready.");
    expect(container.textContent).not.toContain("Exact arguments");
    expect(container.querySelector('[data-testid="action-execution-outcome"]')?.textContent).not.toContain("Result");
    expect(
      container.querySelector('a[href="https://mail.google.com/mail/u/0/#drafts?compose=draft-123"]')
    ).not.toBeNull();

    const raw = container.querySelector<HTMLInputElement>('input[type="checkbox"]');
    expect(raw).not.toBeNull();
    await act(async () => raw!.click());
    expect(container.textContent).toContain("Exact arguments");
    expect(container.textContent).toContain('"subject": "Release notes"');
    expect(container.textContent).toContain('"id": "draft-123"');
    expect(container.querySelector('[data-testid="action-execution-outcome"]')?.textContent).toContain("Result");
  });

  it("keeps a sandbox group's result as its stored JSON, without a Raw switch, even one shaped like a CallToolResult", async () => {
    const container = await renderHistory(
      [succeeded("test_sandbox", IMAGE_RESULT)],
      [group("test_sandbox", ExecutorKind.POD)]
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

it("renders native sandbox results and keeps the original payload available through Raw", async () => {
  const row = request(ActionState.SUCCEEDED, 1);
  row.action = create(ActionIdentitySchema, { group: "sandbox", name: "dispose" });
  row.arguments = { name: "absent-sandbox" };
  row.execution!.result = fromJson(ValueSchema, { name: "absent-sandbox", existed: false });
  const container = await renderHistory([row], [group("sandbox", ExecutorKind.POD)]);
  expect(container.textContent).toContain("already absent");
  const raw = container.querySelector<HTMLInputElement>('input[type="checkbox"]');
  expect(raw).not.toBeNull();
  await act(async () => raw?.click());
  expect(container.textContent).toContain('"existed"');
  expect(container.textContent).not.toContain("already absent");
});
