import { afterEach, describe, expect, it, vi } from "vitest";

import { callToolRequest, fetchRuns, lookupToolRequestCall, sendFeedback } from "./client.ts";
import { resetRepoCache } from "./repo.ts";

// Stub /api/repo/tree + /api/repo/blobs with a given tree and sha→content map.
function stubRepo(tree: unknown, blobs: Record<string, string>) {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      if (url === "/api/repo/tree") return Promise.resolve({ ok: true, json: () => Promise.resolve(tree) } as Response);
      const shas = new URL(url, "http://x").searchParams.get("shas")!.split(",");
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve(shas.map((s) => ({ sha: s, content: blobs[s] }))),
      } as Response);
    })
  );
}

describe("fetchRuns (one .md per run: manifest frontmatter + notes body)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    resetRepoCache();
  });

  const RUNS_TREE = {
    sha: "h",
    entries: [
      { path: "runs/2026-06-29/01RUNAAA.md", type: "blob", sha: "a" },
      { path: "runs/2026-06-28/01RUNBBB.md", type: "blob", sha: "b" }, // older
      { path: "runs/README.md", type: "blob", sha: "readme" }, // no frontmatter → dropped
    ],
  };
  const RUNS_BLOBS: Record<string, string> = {
    a:
      "---\nrun_id: 01RUNAAA\nstarted: '2026-06-29T09:08:00-07:00'\n" +
      "sources:\n  - {source: gmail, changes_seen: 2}\n  - {source: tana, skipped: 'no egress'}\n" +
      "---\n## Run notes\n\nQuiet run.",
    b: "---\nrun_id: 01RUNBBB\nstarted: '2026-06-28T08:00:00-07:00'\n---\n", // empty body
    readme: "# Runs\n\nPer-run propagation record. (prose, no frontmatter)",
  };

  it("reads each run's frontmatter + body, drops README, sorts newest-first", async () => {
    stubRepo(RUNS_TREE, RUNS_BLOBS);
    const { runs } = await fetchRuns();
    expect(runs.map((r) => r.run_id)).toEqual(["01RUNAAA", "01RUNBBB"]); // newest `started` first
    expect(runs[0].notes_md).toMatch(/^## Run notes/); // body → notes
    expect(runs[0].sources.map((s) => s.source)).toEqual(["gmail", "tana"]);
    expect("skipped" in runs[0].sources[1]).toBe(true); // discriminated union preserved
    expect(runs[1].notes_md).toBe(""); // B has an empty body
    expect(runs[1].propagation).toEqual([]); // omitted section → default
  });
});

describe("sendFeedback", () => {
  afterEach(() => vi.unstubAllGlobals());

  function stubOk() {
    const fetchMock = vi.fn(() => Promise.resolve({ ok: true } as Response));
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }
  const bodyOf = (fetchMock: ReturnType<typeof vi.fn>) =>
    JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string);

  it("posts the note text with a null item_id and no page/selection when no context is given", async () => {
    const fetchMock = stubOk();
    await sendFeedback("just a note");
    expect(fetchMock).toHaveBeenCalledWith("/api/trace/feedback", expect.objectContaining({ method: "POST" }));
    expect(bodyOf(fetchMock)).toEqual({ text: "just a note", item_id: null });
  });

  it("includes the page and selected text when a context is supplied", async () => {
    const fetchMock = stubOk();
    await sendFeedback("this page looks bad", undefined, { page: "#/runs", selection: "6 scanned · 2 skipped" });
    expect(bodyOf(fetchMock)).toEqual({
      text: "this page looks bad",
      item_id: null,
      page: "#/runs",
      selection: "6 scanned · 2 skipped",
    });
  });

  it("sends the page but omits selection when nothing was selected", async () => {
    const fetchMock = stubOk();
    await sendFeedback("note", undefined, { page: "#/inbox", selection: null });
    expect(bodyOf(fetchMock)).toEqual({ text: "note", item_id: null, page: "#/inbox" });
  });
});

describe("callToolRequest", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    resetRepoCache();
  });

  const requestYaml = `
state_request_id: 2026-07-thrive.box
server_id: grocy-sf
tool_name: stock_add
title: Add Thrive box to Grocy
rationale: box present
arguments:
  items:
    - product_id: 123
      amount: 1
`;

  function stubToolRequestFetch() {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url === "/api/repo/tree")
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({
              sha: "head",
              entries: [{ path: "tool_requests/2026-07-thrive.box.yaml", type: "blob", sha: "abc123" }],
            }),
        } as Response);
      if (url === "/api/repo/blobs?shas=abc123")
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve([{ sha: "abc123", content: requestYaml }]),
        } as Response);
      if (url === "/api/tool-calls" || url === "/api/tool-calls/lookup")
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ tool_call_id: "tc_1", server_id: "grocy-sf", status: "approval_required" }),
        } as Response);
      throw new Error(`unexpected fetch: ${url} ${JSON.stringify(init)}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("loads the authored request and posts its exact body to the backend proxy", async () => {
    const fetchMock = stubToolRequestFetch();
    const record = await callToolRequest("2026-07-thrive.box", 500);
    const toolCall = fetchMock.mock.calls.find((c) => c[0] === "/api/tool-calls");
    expect(toolCall).toBeTruthy();
    const init = toolCall![1] as RequestInit;
    expect(JSON.parse(init.body as string)).toEqual({
      state_request_id: "2026-07-thrive.box",
      server_id: "grocy-sf",
      tool_name: "stock_add",
      title: "Add Thrive box to Grocy",
      rationale: "box present",
      arguments: { items: [{ product_id: 123, amount: 1 }] },
      wait_for_ms: 500,
    });
    expect(record.status).toBe("approval_required");
  });

  it("uses the same request body for console state lookup", async () => {
    const fetchMock = stubToolRequestFetch();
    const record = await lookupToolRequestCall("2026-07-thrive.box");
    const lookup = fetchMock.mock.calls.find((c) => c[0] === "/api/tool-calls/lookup");
    expect(lookup).toBeTruthy();
    const init = lookup![1] as RequestInit;
    expect(JSON.parse(init.body as string).state_request_id).toBe("2026-07-thrive.box");
    expect(record?.tool_call_id).toBe("tc_1");
  });
});
