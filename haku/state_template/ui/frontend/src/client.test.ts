import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchRuns, sendFeedback } from "./client.ts";
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

describe("fetchRuns (composed over the shared git-store reader)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    resetRepoCache();
  });

  const RUNS_TREE = {
    sha: "h",
    entries: [
      { path: "runs/2026-06-29/01RUNAAA.yaml", type: "blob", sha: "a-yaml" },
      { path: "runs/2026-06-29/01RUNAAA.md", type: "blob", sha: "a-md" },
      { path: "runs/2026-06-28/01RUNBBB.yaml", type: "blob", sha: "b-yaml" }, // no .md sibling
      { path: "runs/README.md", type: "blob", sha: "readme" }, // ignored
      { path: "items/01X.yaml", type: "blob", sha: "item" }, // ignored (not a run)
    ],
  };
  const RUNS_BLOBS: Record<string, string> = {
    "a-yaml":
      "run_id: 01RUNAAA\nstarted: '2026-06-29T09:08:00-07:00'\n" +
      "sources:\n  - {source: gmail, changes_seen: 2}\n  - {source: tana, skipped: 'no egress'}\n",
    "a-md": "## Run notes\n\nQuiet run.",
    "b-yaml": "run_id: 01RUNBBB\nstarted: '2026-06-28T08:00:00-07:00'\n", // older; no md
  };

  it("pairs each run yaml with its sibling md, ignores README/non-runs, sorts newest-first", async () => {
    stubRepo(RUNS_TREE, RUNS_BLOBS);
    const { runs } = await fetchRuns();
    expect(runs.map((r) => r.run_id)).toEqual(["01RUNAAA", "01RUNBBB"]); // newest `started` first
    expect(runs[0].notes_md).toMatch(/^## Run notes/);
    expect(runs[0].sources.map((s) => s.source)).toEqual(["gmail", "tana"]);
    expect("skipped" in runs[0].sources[1]).toBe(true); // discriminated union preserved
    expect(runs[1].notes_md).toBe(""); // B has no .md sibling
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
