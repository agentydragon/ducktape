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

// --- Runs surface (runs/<date>/<ulid>.md) ---
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

// Operator-approved tool calls. haku-state stores the authored request under
// tool_requests/*.yaml; haku-console owns authorization, execution, audit, and results.
export type ToolCallStatus = "approval_required" | "running" | "ok" | "error" | "denied";

export interface ToolRequestDoc {
  state_request_id: string;
  server_id: string;
  tool_name: string;
  title: string;
  rationale?: string;
  arguments?: Record<string, unknown>;
}

interface ToolCallIdentity {
  tool_call_id: string;
  server_id: string;
  server_title?: string;
  tool_name?: string;
}

interface ToolCallAuditFields {
  caller_principal?: string;
  status: ToolCallStatus;
  created_at?: string;
  updated_at?: string;
}

interface ToolCallRequestEcho {
  arguments?: Record<string, unknown>;
  rationale?: string;
  request_title?: string | null;
  client_request_id?: string | null;
  state_request_id?: string | null;
  request_digest?: string;
}

interface ToolCallDecisionFields {
  approval_id?: string | null;
  decision_reason?: string | null;
  result?: Record<string, unknown> | null;
  error?: string | null;
}

export type ToolCallRecord = ToolCallIdentity & ToolCallAuditFields & ToolCallRequestEcho & ToolCallDecisionFields;

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
