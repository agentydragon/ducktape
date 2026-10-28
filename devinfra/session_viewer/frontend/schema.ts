export type Harness = "claude" | "codex";
export type TranscriptStatus = "captured" | "pending" | "failed" | "unavailable";

export type Machine = {
  id: string;
  display_name: string;
};

export type SnapshotImportState =
  "inventory_pending" | "inventory_failed" | "processing_pending" | "processing_failed" | "complete";

export type MachineImportProgress = {
  machine_id: string;
  display_name: string;
  known_snapshot_count: number;
  completed_snapshot_count: number;
  failed_snapshot_count: number;
  oldest_unfinished_snapshot_at: string | null;
  oldest_unfinished_snapshot_state: SnapshotImportState | null;
  processed_file_count: number;
  pending_file_count: number;
  failed_file_count: number;
};

export type SessionSummary = {
  id: string;
  machine_id: string;
  harness: Harness;
  title: string | null;
  cwd: string | null;
  started_at: string | null;
  last_activity_at: string | null;
  latest_version_id: string | null;
  version_count: number;
  latest_version_record_count: number | null;
  transcript_status: TranscriptStatus;
  parent_session_id: string | null;
  parent_link_kind: string | null;
  parent_native_session_id: string | null;
  identity_inferred: boolean;
  native_session_id_conflict?: boolean;
  native_session_id_candidates?: string[];
  parent_native_session_id_conflict?: boolean;
  parent_native_session_id_candidates?: string[];
};

export type SessionVersion = {
  id: string;
  captured_at: string;
  sha256: string;
  byte_size: number;
  source_path: string;
  complete: boolean;
  parse_issue_count: number;
};

/** One physical JSONL line in its original order, including lines that did not parse. */
export type EventRecord = {
  ordinal: number;
  timestamp: string | null;
  kind: string | null;
  parsed: unknown | null;
  raw_base64: string;
  parse_error?: string | null;
};

export type SessionPage<T> = {
  data: T[];
  next_cursor: string | null;
};

export type EventPage<T> = {
  data: T[];
  next_cursor: number | null;
};

export type SessionQuery = {
  machineId?: string;
  harness?: Harness;
  cursor?: string;
  limit?: number;
  parentSessionId?: string;
};
