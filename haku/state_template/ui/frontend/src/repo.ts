// Frontend client for the backend's generic content proxy — Forgejo's two read primitives
// (recursive tree + bulk blob fetch, see ui/backend/app.py). The composition the view widgets
// build on: `docsUnder` loads every .md under a directory in exactly two calls (one tree, one
// bulk blobs), parsing each file's frontmatter. New directories/views need no backend work.
//
// Read caches (content-addressing makes this nearly free): blobs are immutable by sha, so they're
// cached permanently and persisted across reloads (blob_cache.ts: memory + localStorage). The tree
// (HEAD's entries) is fetched as "current HEAD", not by sha — so it can't be a by-sha persistent
// cache — and is kept only in-memory with a short TTL, invalidated on any local write (client.ts
// write helpers call `invalidateTree`) so a write's new blob sha is discovered on the next read.
// Concurrent callers share one in-flight tree fetch — the common case where several widgets mount
// at once and each want the tree.

import { clearBlobCache, getBlob, hasBlob, setBlob } from "./blob_cache.ts";
import { type FrontMatter, parseFrontmatter } from "./frontmatter.ts";
import { gitProgress } from "./git_progress.ts";
import type { RepoBlob, RepoTree, RepoTreeEntry } from "./types.ts";

let treeInFlight: Promise<RepoTree> | null = null;
let treeAt = 0;
const TREE_TTL_MS = 15_000;

// Drop the cached tree so the next read re-fetches HEAD (called after a write moves HEAD).
export function invalidateTree(): void {
  treeInFlight = null;
  treeAt = 0;
}

// Full reset (tree + the immutable blob cache, memory + persisted) — for tests and a session reset.
export function resetRepoCache(): void {
  invalidateTree();
  clearBlobCache();
}

export function repoTree(): Promise<RepoTree> {
  if (treeInFlight && Date.now() - treeAt < TREE_TTL_MS) return treeInFlight;
  treeAt = Date.now();
  // A tree read is one git object; the shared in-flight promise means concurrent callers
  // ride one fetch, so it registers with the tracker exactly once.
  treeInFlight = gitProgress.track(1, async () => {
    const res = await fetch("/api/repo/tree");
    if (!res.ok) {
      invalidateTree(); // don't cache a failure
      throw new Error(`repo tree: ${res.status}`);
    }
    return (await res.json()) as RepoTree;
  });
  return treeInFlight;
}

// Blobs by sha, in input order. Content-addressed → cached permanently; only uncached shas fetched.
// The backend returns every requested blob or errors (it chunks below Forgejo's 50-blob cap and
// raises on a short response — see ui/backend/forgejo.py), so a sha still missing after the fetch
// means a truncation slipped through (a proxy dropping the query, say). Surface it — never yield ""
// for a dropped blob, which would silently render an item blank.
export async function repoBlobs(shas: string[]): Promise<RepoBlob[]> {
  const missing = shas.filter((s) => !hasBlob(s)); // hasBlob promotes a persisted hit into memory
  if (missing.length > 0) {
    await gitProgress.track(missing.length, async () => {
      const res = await fetch(`/api/repo/blobs?shas=${missing.join(",")}`);
      if (!res.ok) throw new Error(`repo blobs: ${res.status}`);
      for (const b of (await res.json()) as RepoBlob[]) setBlob(b.sha, b.content);
    });
    const dropped = missing.filter((s) => !hasBlob(s));
    if (dropped.length > 0)
      throw new Error(
        `repo blobs: ${dropped.length}/${missing.length} missing from response (${dropped.slice(0, 3).join(", ")})`
      );
  }
  return shas.map((sha) => ({ sha, content: getBlob(sha)! }));
}

// One repo blob resolved to its path + text content — the unit `readBlobs` yields.
export interface Blob {
  path: string;
  content: string;
}

// We read blobs in ≤50-sha chunks: it bounds the request URL (no proxy silently truncating a huge
// query string) and doubles as the batching unit for streaming partial renders. The backend chunks
// again below Forgejo's own 50-blob cap.
const BLOB_CHUNK = 50;

// THE shared high-level read over the git store: the text of every tree blob matching `select`, in
// one tree read + chunked bulk-blob reads (all tracked by git_progress.ts, none silently dropped —
// see repoBlobs). Blobs come back in tree order; the optional `onBatch` fires after each chunk with
// everything read so far, for progressive rendering. Every surface — items, garden, kitchen, runs,
// responses — composes over this instead of re-implementing tree-filtering, sha-mapping, chunking.
export async function readBlobs(
  select: (e: RepoTreeEntry) => boolean,
  onBatch?: (soFar: Blob[]) => void
): Promise<Blob[]> {
  const entries = (await repoTree()).entries.filter((e) => e.type === "blob" && select(e));
  const out: Blob[] = [];
  for (let i = 0; i < entries.length; i += BLOB_CHUNK) {
    const chunk = entries.slice(i, i + BLOB_CHUNK);
    const bySha = new Map((await repoBlobs(chunk.map((e) => e.sha))).map((b) => [b.sha, b.content] as const));
    for (const e of chunk) out.push({ path: e.path, content: bySha.get(e.sha)! });
    onBatch?.(out);
  }
  return out;
}

// One file's text by exact repo path, or `null` if the path isn't a blob in the tree.
export async function repoFile(path: string): Promise<string | null> {
  const [blob] = await readBlobs((e) => e.path === path);
  return blob?.content ?? null;
}

export interface Doc extends FrontMatter {
  path: string;
}

// Streaming hook for `docsUnder`: `onDocs` gets the accumulated (path-sorted) docs after each blob
// chunk so a view can render items as they stream in. Load progress itself is not reported here —
// the git-object transfer is tracked centrally (git_progress.ts) and shown by the one global bar.
export interface DocsUnderOpts {
  onDocs?: (docs: Doc[]) => void;
}

const toDocs = (blobs: Blob[]): Doc[] =>
  blobs.map((b) => ({ path: b.path, ...parseFrontmatter(b.content) })).sort((a, b) => a.path.localeCompare(b.path));

// Every `.md` file directly-or-deeper under `dir` with its parsed frontmatter + body, path-sorted.
// The trailing "/" on the dir match keeps a sibling like `memory/improvements-archive` out of
// `memory/improvements`. Composes over readBlobs; `onDocs` streams the accumulated set per chunk.
export async function docsUnder(dir: string, opts: DocsUnderOpts = {}): Promise<Doc[]> {
  const blobs = await readBlobs(
    (e) => e.path.endsWith(".md") && e.path.startsWith(`${dir}/`),
    opts.onDocs ? (soFar) => opts.onDocs!(toDocs(soFar)) : undefined
  );
  return toDocs(blobs);
}
