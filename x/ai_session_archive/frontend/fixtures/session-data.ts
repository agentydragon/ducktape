import type { SessionBrowserApi } from "../api";
import type { EventRecord, Machine, MachineImportProgress, SessionSummary, SessionVersion } from "../schema";
import { codexWorkRecords } from "./codex-work";
import { claudeWorkRecords } from "./claude-work";

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
    known_snapshot_count: 198,
    completed_snapshot_count: 126,
    failed_snapshot_count: 0,
    codex_metadata_pending_snapshot_count: 69,
    codex_metadata_failed_snapshot_count: 0,
    codex_metadata_partial_snapshot_count: 0,
    oldest_unfinished_snapshot_at: "2026-10-28T17:00:00Z",
    oldest_unfinished_snapshot_state: "codex_metadata_pending",
    processed_file_count: 5574,
    pending_file_count: 0,
    failed_file_count: 0,
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
    session_kind: "main",
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
    session_kind: "subagent",
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
    id: "guardian-session",
    machine_id: "thinkpad",
    harness: "codex",
    session_kind: "guardian",
    title: "Guardian automation fixture",
    cwd: null,
    started_at: null,
    last_activity_at: null,
    latest_version_id: null,
    version_count: 0,
    latest_version_record_count: null,
    transcript_status: "unavailable",
    parent_session_id: "codex-thread-1",
    parent_link_kind: "codex_parent_thread_id",
    parent_native_session_id: "thread-one",
    identity_inferred: false,
  },
  {
    id: "inferred-claude-parent",
    machine_id: "atlas",
    harness: "claude",
    session_kind: "main",
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
  {
    id: "mixed-transcript-fixture",
    machine_id: "thinkpad",
    harness: "codex",
    session_kind: "main",
    title: "Tools and reasoning fixture",
    cwd: "/fixture/project",
    started_at: "2026-10-28T16:00:00Z",
    last_activity_at: "2026-10-28T16:25:00Z",
    latest_version_id: "mixed-version",
    version_count: 1,
    latest_version_record_count: 9,
    transcript_status: "captured",
    parent_session_id: null,
    parent_link_kind: null,
    parent_native_session_id: null,
    identity_inferred: false,
  },
  {
    id: "codex-work-fixture",
    machine_id: "thinkpad",
    harness: "codex",
    session_kind: "main",
    title: "Archive transcript UI work",
    cwd: "/fixture/project",
    started_at: "2026-10-31T06:00:00Z",
    last_activity_at: "2026-10-31T06:30:00Z",
    latest_version_id: "codex-work-version",
    version_count: 1,
    latest_version_record_count: codexWorkRecords.length,
    transcript_status: "captured",
    parent_session_id: null,
    parent_link_kind: null,
    parent_native_session_id: null,
    identity_inferred: false,
  },
  {
    id: "claude-attachments-fixture",
    machine_id: "atlas",
    harness: "claude",
    session_kind: "main",
    title: "Claude attachment disclosures",
    cwd: "/fixture/project",
    started_at: null,
    last_activity_at: null,
    latest_version_id: "claude-attachments-version",
    version_count: 1,
    latest_version_record_count: 4,
    transcript_status: "captured",
    parent_session_id: null,
    parent_link_kind: null,
    parent_native_session_id: null,
    identity_inferred: false,
  },
  {
    id: "claude-work-fixture",
    machine_id: "atlas",
    harness: "claude",
    session_kind: "main",
    title: "Claude paired tools and mixed messages",
    cwd: "/fixture/project",
    started_at: null,
    last_activity_at: null,
    latest_version_id: "claude-work-version",
    version_count: 1,
    latest_version_record_count: claudeWorkRecords.length,
    transcript_status: "captured",
    parent_session_id: null,
    parent_link_kind: null,
    parent_native_session_id: null,
    identity_inferred: false,
  },
];

const versions: Record<string, SessionVersion[]> = {
  "claude-work-fixture": [
    {
      id: "claude-work-version",
      captured_at: "2026-10-31T06:30:00Z",
      sha256: "b".repeat(64),
      byte_size: 2000,
      source_path: "/fixture/claude-work.jsonl",
      complete: true,
      parse_issue_count: 0,
    },
  ],
  "claude-attachments-fixture": [
    {
      id: "claude-attachments-version",
      captured_at: "2026-10-31T06:30:00Z",
      sha256: "a".repeat(64),
      byte_size: 1000,
      source_path: "/fixture/claude.jsonl",
      complete: true,
      parse_issue_count: 0,
    },
  ],
  "codex-work-fixture": [
    {
      id: "codex-work-version",
      captured_at: "2026-10-31T06:30:00Z",
      sha256: "f".repeat(64),
      byte_size: 5000,
      source_path: "/fixture/work.jsonl",
      complete: true,
      parse_issue_count: 0,
    },
  ],
  "mixed-transcript-fixture": [
    {
      id: "mixed-version",
      captured_at: "2026-10-28T16:26:00Z",
      sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      byte_size: 5000,
      source_path: "/fixture/session.jsonl",
      complete: true,
      parse_issue_count: 0,
    },
  ],

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

const mixedEvents: EventRecord[] = [
  record(1, { type: "session_meta", payload: { id: "fixture-thread", cwd: "/fixture/project" } }, "session_meta"),
  record(
    2,
    {
      type: "response_item",
      payload: {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: "Inspect the archived build failure and explain the result." }],
      },
    },
    "response_item"
  ),
  record(
    3,
    {
      type: "response_item",
      payload: {
        type: "reasoning",
        summary: [{ type: "summary_text", text: "Check the failing target and its captured log." }],
      },
    },
    "response_item"
  ),
  record(
    4,
    {
      type: "response_item",
      payload: {
        type: "message",
        role: "assistant",
        phase: "commentary",
        content: [{ type: "output_text", text: "I will inspect the build log." }],
      },
    },
    "response_item"
  ),
  record(
    5,
    {
      type: "response_item",
      payload: {
        type: "function_call",
        call_id: "build-log",
        name: "read_build_log",
        arguments: '{"target":"//fixture:test"}',
      },
    },
    "response_item"
  ),
  record(6, { type: "event_msg", payload: { type: "token_count", info: { total: 128 } } }, "event_msg"),
  record(
    7,
    {
      type: "response_item",
      payload: {
        type: "function_call_output",
        call_id: "build-log",
        output: "Fixture build output: missing configuration for the test environment.",
      },
    },
    "response_item"
  ),
  record(
    8,
    {
      type: "response_item",
      payload: {
        type: "message",
        role: "assistant",
        phase: "final_answer",
        content: [
          {
            type: "output_text",
            text: "The archived failure comes from **missing test configuration**.\n\n```yaml\nfixture_enabled: true\n```\n\nThe captured output identifies the missing setting; the transcript does not show a subsequent rerun.",
          },
        ],
      },
    },
    "response_item"
  ),
  record(9, { type: "event_msg", payload: { type: "task_complete" } }, "event_msg"),
];

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

const claudeAttachmentEvents: EventRecord[] = [
  record(1, { type: "user", message: { content: "Inspect the attached changes." } }, "user"),
  record(
    2,
    {
      type: "attachment",
      attachment: {
        type: "hook_success",
        hookName: "PostToolUse",
        stdout: "fixture hook completed",
        stderr: "",
        exitCode: 0,
      },
    },
    "attachment"
  ),
  record(
    3,
    {
      type: "attachment",
      attachment: { type: "edited_text_file", filename: "/fixture/config.ts", snippet: "export const enabled = true;" },
    },
    "attachment"
  ),
  record(
    4,
    { type: "assistant", message: { content: "The captured attachment records are available for inspection." } },
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
        (query.kinds === undefined || query.kinds.includes(session.session_kind)) &&
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
      sessionId === "claude-work-fixture"
        ? claudeWorkRecords
        : sessionId === "claude-attachments-fixture"
          ? claudeAttachmentEvents
          : sessionId === "codex-work-fixture"
            ? codexWorkRecords
            : sessionId === "mixed-transcript-fixture"
              ? mixedEvents
              : sessionId === "codex-thread-1"
                ? versionId === "codex-version-1"
                  ? codexOlderEvents
                  : codexLatestEvents
                : claudeEvents;
    const data = selected
      .filter((record) => cursor === undefined || record.ordinal < cursor)
      .slice(
        -([
          "mixed-transcript-fixture",
          "codex-work-fixture",
          "claude-attachments-fixture",
          "claude-work-fixture",
        ].includes(sessionId)
          ? 50
          : 3)
      );
    const first = data.at(0)?.ordinal ?? null;
    return {
      data,
      next_cursor: first !== null && selected.some((record) => record.ordinal < first) ? first : null,
    };
  },
};

const longRelatedChildren: SessionSummary[] = Array.from({ length: 63 }, (_unused, index) => ({
  id: `claude-child-${String(index + 1).padStart(2, "0")}`,
  machine_id: "atlas",
  harness: "claude",
  session_kind: "subagent",
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
