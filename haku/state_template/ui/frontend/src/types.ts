// Wire contract with the backend's JSON API. Mirrors the Pydantic models in
// ../../backend/models.py — keep them in sync by hand. Items are read from `items/*.md` through the
// generic content proxy (repo.ts composes tree+blobs), not a bespoke wire type.

// Context snapshotted when the operator opens a "Note to Haku" — which page they're on and any
// text they had selected — so Haku has grounding when the note says e.g. "this page looks bad".
export interface FeedbackContext {
  page: string; // the URL hash, e.g. "#/runs"
  selection: string | null; // selected text at note-open time, or null if none
}

// Footer metadata (GET /api/meta): freshness + which image is serving.
export interface MetaResponse {
  scan_time: string; // ISO 8601 of the last Haku commit ("last scan")
  deployed_commit: string | null; // short SHA the running image was built from
  deployed_commit_url: string | null; // Forgejo link to that commit
}

// Improvements are a content collection (memory/improvements/<id>.md) rendered by the
// <improvement-board/> widget — no wire type; the widget parses frontmatter itself.

// --- Runs surface (runs/<date>/<ulid>.{yaml,md}) ---
// Per-run propagation record: each source processed + how each change reached every surface.
// A source was either scanned (bookmarks + count) or skipped (reason) — a discriminated union
// (mirrors the backend's ScannedSource | SkippedSource); discriminate on the `skipped` key.
export interface ScannedSource {
  source: string;
  bookmark_before: string | number | null;
  bookmark_after: string | number | null;
  changes_seen: number | string;
}

export interface SkippedSource {
  source: string;
  skipped: string;
}

export type RunSource = ScannedSource | SkippedSource;

export interface RunChecklist {
  checklist: string;
  ref: string;
  walked: boolean;
}

export interface PropagationTarget {
  surface: string;
  action: "created" | "updated" | "no_change" | "n/a";
  note: string;
}

export interface PropagationEntry {
  change: string;
  source: string;
  surfaces: PropagationTarget[];
}

export interface RunManifest {
  run_id: string;
  date: string;
  started: string;
  finished: string;
  sources: RunSource[];
  checklists: RunChecklist[];
  propagation: PropagationEntry[];
  notes_md: string;
}

export interface RunsResponse {
  runs: RunManifest[];
}

// The knowledge garden browses/reads arbitrary repo markdown through the generic content proxy
// below (repo.ts composes tree+blobs) — no dedicated garden wire types.

// Generic content proxy — mirrors ui/backend/models.py (Forgejo's tree + bulk-blobs primitives).
export interface RepoTreeEntry {
  path: string;
  type: string; // git object type: "blob" | "tree"
  sha: string;
}

export interface RepoTree {
  sha: string; // the HEAD commit the tree was read at
  entries: RepoTreeEntry[];
}

export interface RepoBlob {
  sha: string;
  content: string;
}
