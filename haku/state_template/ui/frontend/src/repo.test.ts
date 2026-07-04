import { afterEach, describe, expect, it, vi } from "vitest";

import { docsUnder, invalidateTree, repoBlobs, repoTree, resetRepoCache } from "./repo.ts";

const treeCalls = (m: ReturnType<typeof vi.fn>) => m.mock.calls.filter((c) => c[0] === "/api/repo/tree").length;
const blobCalls = (m: ReturnType<typeof vi.fn>) => m.mock.calls.filter((c) => String(c[0]).includes("/blobs")).length;

const TREE = {
  sha: "head",
  entries: [
    { path: "memory/improvements/beta.md", type: "blob", sha: "b2" },
    { path: "memory/improvements/alpha.md", type: "blob", sha: "a1" },
    { path: "memory/improvements/note.txt", type: "blob", sha: "c3" }, // not .md
    { path: "memory/improvements-archive/old.md", type: "blob", sha: "d4" }, // prefix-substring sibling
    { path: "memory/improvements", type: "tree", sha: "t0" }, // a tree, not a blob
  ],
};
const BLOBS: Record<string, string> = {
  a1: "---\nkind: improvement\ntitle: Alpha\n---\nalpha",
  b2: "---\nkind: improvement\ntitle: Beta\n---\nbeta",
};

function mockFetch() {
  return vi.fn((url: string) => {
    if (url === "/api/repo/tree") {
      return Promise.resolve({ ok: true, json: () => Promise.resolve(TREE) } as Response);
    }
    const shas = new URL(url, "http://x").searchParams.get("shas")!.split(",");
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve(shas.map((s) => ({ sha: s, content: BLOBS[s] }))),
    } as Response);
  });
}

describe("docsUnder + read caches", () => {
  // Reset the session caches (tree + blobs) between tests so call-count assertions start clean.
  afterEach(() => {
    resetRepoCache();
    vi.unstubAllGlobals();
  });

  it("loads .md under the dir in two calls, parses frontmatter, excludes siblings, sorts by path", async () => {
    const fetchMock = mockFetch();
    vi.stubGlobal("fetch", fetchMock);
    const entries = await docsUnder("memory/improvements");
    expect(entries.map((e) => e.path)).toEqual(["memory/improvements/alpha.md", "memory/improvements/beta.md"]);
    expect(entries[0].data).toEqual({ kind: "improvement", title: "Alpha" });
    expect(entries[0].body).toBe("alpha");
    // exactly two requests: one tree, one bulk blobs (sha order follows the tree, not the
    // final path sort — beta then alpha here; the path sort is applied to the returned entries)
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toContain("/api/repo/blobs?shas=b2,a1");
  });

  it("repoBlobs short-circuits on an empty sha list (no request)", async () => {
    const fetchMock = mockFetch();
    vi.stubGlobal("fetch", fetchMock);
    expect(await repoBlobs([])).toEqual([]);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("throws with the status on a non-ok tree response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve({ ok: false, status: 500 } as Response))
    );
    await expect(repoTree()).rejects.toThrow("repo tree: 500");
  });

  it("caches the tree across calls and dedupes concurrent reads (one fetch)", async () => {
    const fetchMock = mockFetch();
    vi.stubGlobal("fetch", fetchMock);
    const [a, b] = await Promise.all([repoTree(), repoTree()]); // concurrent → shared in-flight
    expect(a).toBe(b);
    await repoTree(); // subsequent → cache hit
    expect(treeCalls(fetchMock)).toBe(1);
  });

  it("re-fetches the tree after invalidateTree()", async () => {
    const fetchMock = mockFetch();
    vi.stubGlobal("fetch", fetchMock);
    await repoTree();
    invalidateTree();
    await repoTree();
    expect(treeCalls(fetchMock)).toBe(2);
  });

  it("caches blobs by sha — a repeat request for the same sha fetches nothing", async () => {
    const fetchMock = mockFetch();
    vi.stubGlobal("fetch", fetchMock);
    expect((await repoBlobs(["a1"]))[0].content).toContain("Alpha");
    await repoBlobs(["a1"]);
    expect(blobCalls(fetchMock)).toBe(1);
  });

  it("throws — never yields '' — when the backend omits a requested blob (no silent truncation)", async () => {
    // Backend responds 200 but drops one of the two requested shas (a proxy truncating the query,
    // a short git/blobs response that slipped the backend's guard, …).
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve([{ sha: "a1", content: "x" }]) } as Response))
    );
    await expect(repoBlobs(["a1", "b2"])).rejects.toThrow(/missing from response/);
  });
});
