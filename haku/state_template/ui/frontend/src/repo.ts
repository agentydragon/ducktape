// Frontend client for the backend's generic content proxy — Forgejo's two read primitives
// (recursive tree + bulk blob fetch, see ui/backend/app.py). The composition the view widgets
// build on: `docsUnder` loads every .md under a directory in exactly two calls (one tree, one
// bulk blobs), parsing each file's frontmatter. New directories/views need no backend work.
//
// Session read caches (content-addressing makes this nearly free): blobs are immutable by sha, so
// they're cached permanently; the tree (HEAD's entries) is cached with a short TTL and invalidated
// on any local write (client.ts write helpers call `invalidateTree`), so a write's new blob sha is
// discovered on the next read. Concurrent callers share one in-flight tree fetch — the common case
// where several widgets mount at once and each want the tree.

import { type FrontMatter, parseFrontmatter } from "./frontmatter.ts";
import type { RepoBlob, RepoTree } from "./types.ts";

const blobCache = new Map<string, string>();
let treeInFlight: Promise<RepoTree> | null = null;
let treeAt = 0;
const TREE_TTL_MS = 15_000;

// Drop the cached tree so the next read re-fetches HEAD (called after a write moves HEAD).
export function invalidateTree(): void {
  treeInFlight = null;
  treeAt = 0;
}

// Full reset (tree + the immutable blob cache) — for tests, and available for a session/HEAD reset.
export function resetRepoCache(): void {
  invalidateTree();
  blobCache.clear();
}

export function repoTree(): Promise<RepoTree> {
  if (treeInFlight && Date.now() - treeAt < TREE_TTL_MS) return treeInFlight;
  treeAt = Date.now();
  treeInFlight = (async () => {
    const res = await fetch("/api/repo/tree");
    if (!res.ok) {
      invalidateTree(); // don't cache a failure
      throw new Error(`repo tree: ${res.status}`);
    }
    return (await res.json()) as RepoTree;
  })();
  return treeInFlight;
}

// Blobs by sha, in input order. Content-addressed → cached permanently; only uncached shas are fetched.
export async function repoBlobs(shas: string[]): Promise<RepoBlob[]> {
  const missing = shas.filter((s) => !blobCache.has(s));
  if (missing.length > 0) {
    const res = await fetch(`/api/repo/blobs?shas=${missing.join(",")}`);
    if (!res.ok) throw new Error(`repo blobs: ${res.status}`);
    for (const b of (await res.json()) as RepoBlob[]) blobCache.set(b.sha, b.content);
  }
  return shas.map((sha) => ({ sha, content: blobCache.get(sha) ?? "" }));
}

// One file's text by exact repo path — resolves its blob sha via the tree, then fetches it.
// `null` if the path isn't a blob in the tree. Both reads hit the caches above.
export async function repoFile(path: string): Promise<string | null> {
  const entry = (await repoTree()).entries.find((e) => e.type === "blob" && e.path === path);
  if (!entry) return null;
  const [blob] = await repoBlobs([entry.sha]);
  return blob?.content ?? null;
}

export interface Doc extends FrontMatter {
  path: string;
}

// Every `.md` file directly-or-deeper under `dir` with its parsed frontmatter + body, sorted by
// path. The trailing "/" on the dir match keeps a sibling like `memory/improvements-archive` out of
// `memory/improvements`.
export async function docsUnder(dir: string): Promise<Doc[]> {
  const tree = await repoTree();
  const under = tree.entries.filter((e) => e.type === "blob" && e.path.endsWith(".md") && e.path.startsWith(`${dir}/`));
  const blobs = await repoBlobs(under.map((e) => e.sha));
  const bySha = new Map(blobs.map((b): [string, string] => [b.sha, b.content]));
  return under
    .map((e) => ({ path: e.path, ...parseFrontmatter(bySha.get(e.sha) ?? "") }))
    .sort((a, b) => a.path.localeCompare(b.path));
}
