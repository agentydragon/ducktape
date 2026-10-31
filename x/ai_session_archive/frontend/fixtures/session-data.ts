import type { SessionBrowserApi } from "../api";
import type { EventRecord, Machine, MachineImportProgress, SessionSummary, SessionVersion } from "../schema";

const machines: Machine[] = [
  { id: "thinkpad", display_name: "ThinkPad" },
  { id: "atlas", display_name: "Atlas" },
  { id: "rugged", display_name: "Rugged" },
];

const importProgress: MachineImportProgress[] = [
  {
    machine_id: "thinkpad",
    display_name: "ThinkPad",
    known_snapshot_count: 5,
    completed_snapshot_count: 3,
    failed_snapshot_count: 0,
    codex_metadata_pending_snapshot_count: 1,
    codex_metadata_failed_snapshot_count: 0,
    codex_metadata_partial_snapshot_count: 1,
    oldest_unfinished_snapshot_at: "2026-10-28T16:30:00Z",
    oldest_unfinished_snapshot_state: "codex_metadata_pending",
    processed_file_count: 27,
    pending_file_count: 4,
    failed_file_count: 0,
  },
  {
    machine_id: "atlas",
    display_name: "Atlas",
    known_snapshot_count: 2,
    completed_snapshot_count: 1,
    failed_snapshot_count: 1,
    codex_metadata_pending_snapshot_count: 0,
    codex_metadata_failed_snapshot_count: 1,
    codex_metadata_partial_snapshot_count: 1,
    oldest_unfinished_snapshot_at: "2026-10-27T18:00:00Z",
    oldest_unfinished_snapshot_state: "codex_metadata_failed",
    processed_file_count: 11,
    pending_file_count: 0,
    failed_file_count: 1,
  },
  {
    machine_id: "rugged",
    display_name: "Rugged",
    known_snapshot_count: 8,
    completed_snapshot_count: 4,
    failed_snapshot_count: 1,
    codex_metadata_pending_snapshot_count: 3,
    codex_metadata_failed_snapshot_count: 0,
    codex_metadata_partial_snapshot_count: 1,
    oldest_unfinished_snapshot_at: "2026-10-26T18:00:00Z",
    oldest_unfinished_snapshot_state: "processing_pending",
    processed_file_count: 42,
    pending_file_count: 9,
    failed_file_count: 2,
  },
  {
    machine_id: "completed-fixture",
    display_name: "Complete fixture machine",
    known_snapshot_count: 2,
    completed_snapshot_count: 2,
    failed_snapshot_count: 0,
    codex_metadata_pending_snapshot_count: 0,
    codex_metadata_failed_snapshot_count: 0,
    codex_metadata_partial_snapshot_count: 1,
    oldest_unfinished_snapshot_at: null,
    oldest_unfinished_snapshot_state: null,
    processed_file_count: 16,
    pending_file_count: 0,
    failed_file_count: 0,
  },
];

const sessions: SessionSummary[] = [
  {
    id: "codex-thread-1",
    machine_id: "thinkpad",
    harness: "codex",
    title: "Compare two captured versions",
    cwd: "/home/example/project",
    started_at: "2026-10-28T16:00:00Z",
    last_activity_at: "2026-10-28T16:25:00Z",
    latest_version_id: "codex-version-2",
    version_count: 2,
    latest_version_record_count: 4,
    transcript_status: "captured",
    parent_session_id: null,
    parent_link_kind: null,
    parent_native_session_id: null,
    identity_inferred: false,
  },
  {
    id: "claude-session-1",
    machine_id: "atlas",
    harness: "claude",
    title: "Inspect a local transcript",
    cwd: "/home/example/another-project",
    started_at: "2026-10-27T18:00:00Z",
    last_activity_at: "2026-10-27T18:45:00Z",
    latest_version_id: "claude-version-1",
    version_count: 1,
    latest_version_record_count: 2,
    transcript_status: "captured",
    parent_session_id: "inferred-claude-parent",
    parent_link_kind: "claude_path_inferred",
    parent_native_session_id: null,
    identity_inferred: false,
  },
  {
    id: "inferred-claude-parent",
    machine_id: "atlas",
    harness: "claude",
    title: "Inferred Claude parent · session 12ab",
    cwd: "/home/example/another-project",
    started_at: null,
    last_activity_at: null,
    latest_version_id: null,
    version_count: 0,
    latest_version_record_count: null,
    transcript_status: "unavailable",
    parent_session_id: null,
    parent_link_kind: null,
    parent_native_session_id: null,
    identity_inferred: true,
  },
];

const versions: Record<string, SessionVersion[]> = {
  "codex-thread-1": [
    {
      id: "codex-version-2",
      captured_at: "2026-10-28T16:26:00Z",
      sha256: "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
      byte_size: 4812,
      source_path: "/home/example/.codex/sessions/2026/10/28/rollout-thread-1.jsonl",
      complete: false,
      parse_issue_count: 1,
    },
    {
      id: "codex-version-1",
      captured_at: "2026-10-28T16:12:00Z",
      sha256: "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
      byte_size: 2670,
      source_path: "/home/example/.codex/sessions/2026/10/28/rollout-thread-1.jsonl",
      complete: true,
      parse_issue_count: 0,
    },
  ],
  "claude-session-1": [
    {
      id: "claude-version-1",
      captured_at: "2026-10-27T18:46:00Z",
      sha256: "fedcba0987654321fedcba0987654321fedcba0987654321fedcba0987654321",
      byte_size: 1991,
      source_path: "/home/example/.claude/projects/project/session-1.jsonl",
      complete: true,
      parse_issue_count: 0,
    },
  ],
};

const codexLatestEvents: EventRecord[] = [
  record(
    1,
    {
      type: "response_item",
      payload: { type: "message", role: "user", content: [{ type: "input_text", text: "Show the earlier capture." }] },
      timestamp: "2026-10-28T16:01:00Z",
    },
    "response_item"
  ),
  record(
    2,
    {
      type: "response_item",
      payload: {
        type: "message",
        role: "assistant",
        content: [{ type: "output_text", text: "I found two snapshots for this session." }],
      },
      timestamp: "2026-10-28T16:02:00Z",
    },
    "response_item"
  ),
  record(3, null, "response_item", '{"type":"response_item","payload":'),
  record(
    4,
    {
      type: "event_msg",
      payload: { type: "context_compacted" },
      timestamp: "2026-10-28T16:20:00Z",
    },
    "event_msg"
  ),
];

const codexOlderEvents: EventRecord[] = [
  record(
    1,
    {
      type: "response_item",
      payload: {
        type: "message",
        role: "assistant",
        content: [{ type: "output_text", text: "This is the earlier captured version." }],
      },
      timestamp: "2026-10-28T16:02:00Z",
    },
    "response_item"
  ),
];

const claudeEvents: EventRecord[] = [
  record(
    1,
    {
      type: "user",
      message: { content: [{ type: "text", text: "Review this local conversation." }] },
      timestamp: "2026-10-27T18:01:00Z",
    },
    "user"
  ),
  record(
    2,
    {
      type: "assistant",
      message: { content: [{ type: "text", text: "The capture preserves the original JSONL records." }] },
      timestamp: "2026-10-27T18:02:00Z",
    },
    "assistant"
  ),
];

export const sessionFixtureApi: SessionBrowserApi = {
  async listMachines() {
    return machines;
  },
  async listImportProgress() {
    return importProgress;
  },
  async listSessions(query) {
    const filtered = sessions.filter(
      (session) =>
        (query.machineId === undefined || session.machine_id === query.machineId) &&
        (query.harness === undefined || session.harness === query.harness) &&
        (query.parentSessionId === undefined || session.parent_session_id === query.parentSessionId)
    );
    return { data: filtered, next_cursor: null };
  },
  async getSession(sessionId) {
    const session = sessions.find((item) => item.id === sessionId);
    if (session === undefined) throw new Error(`Unknown fixture session ${sessionId}`);
    return session;
  },
  async listVersions(sessionId) {
    return versions[sessionId] ?? [];
  },
  async listEvents(sessionId, versionId, cursor) {
    if (sessionId === "inferred-claude-parent") return { data: [], next_cursor: null };
    const selected =
      sessionId === "codex-thread-1"
        ? versionId === "codex-version-1"
          ? codexOlderEvents
          : codexLatestEvents
        : claudeEvents;
    const after = cursor ?? 0;
    const data = selected.filter((record) => record.ordinal > after).slice(0, 3);
    const last = data.at(-1)?.ordinal ?? null;
    return {
      data,
      next_cursor: last !== null && selected.some((record) => record.ordinal > last) ? last : null,
    };
  },
};

const longRelatedChildren: SessionSummary[] = Array.from({ length: 63 }, (_unused, index) => ({
  id: `claude-child-${String(index + 1).padStart(2, "0")}`,
  machine_id: "atlas",
  harness: "claude",
  title: `Inspect module ${String(index + 1).padStart(2, "0")}`,
  cwd: "/home/example/another-project",
  started_at: "2026-10-27T18:00:00Z",
  last_activity_at: "2026-10-27T18:45:00Z",
  latest_version_id: null,
  version_count: 0,
  latest_version_record_count: null,
  transcript_status: "unavailable",
  parent_session_id: "claude-session-1",
  parent_link_kind: "claude_path_inferred",
  parent_native_session_id: null,
  identity_inferred: false,
}));

export const longRelatedSessionFixtureApi: SessionBrowserApi = {
  ...sessionFixtureApi,
  async listSessions(query, signal) {
    if (query.parentSessionId !== "claude-session-1") return sessionFixtureApi.listSessions(query, signal);
    const offset = Number(query.cursor ?? "0");
    const limit = query.limit ?? 50;
    const data = longRelatedChildren.slice(offset, offset + limit);
    const next = offset + data.length;
    return { data, next_cursor: next < longRelatedChildren.length ? String(next) : null };
  },
  async getSession(sessionId, signal) {
    const child = longRelatedChildren.find((session) => session.id === sessionId);
    return child ?? sessionFixtureApi.getSession(sessionId, signal);
  },
};

function record(ordinal: number, parsed: unknown | null, kind: string, malformedRaw?: string): EventRecord {
  const raw = malformedRaw ?? JSON.stringify(parsed);
  return {
    ordinal,
    timestamp:
      typeof parsed === "object" && parsed !== null && "timestamp" in parsed && typeof parsed.timestamp === "string"
        ? parsed.timestamp
        : null,
    kind,
    parsed,
    raw_base64: btoa(raw),
    parse_error: malformedRaw === undefined ? null : "Unexpected end of JSON input",
  };
}
