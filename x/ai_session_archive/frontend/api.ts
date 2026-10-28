import type {
  EventPage,
  EventRecord,
  Harness,
  MachineImportProgress,
  Machine,
  SessionPage,
  SessionQuery,
  SessionSummary,
  SessionVersion,
} from "./schema";

export type SessionBrowserApi = {
  listMachines(signal?: AbortSignal): Promise<Machine[]>;
  listImportProgress(signal?: AbortSignal): Promise<MachineImportProgress[]>;
  listSessions(query: SessionQuery, signal?: AbortSignal): Promise<SessionPage<SessionSummary>>;
  getSession(sessionId: string, signal?: AbortSignal): Promise<SessionSummary>;
  listVersions(sessionId: string, signal?: AbortSignal): Promise<SessionVersion[]>;
  listEvents(
    sessionId: string,
    versionId: string,
    cursor?: number,
    signal?: AbortSignal
  ): Promise<EventPage<EventRecord>>;
};

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    method: "GET",
    credentials: "same-origin",
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    throw new ApiError(response.status, `The local session service answered ${response.status}.`);
  }
  return (await response.json()) as T;
}

function sessionQuery(query: SessionQuery): string {
  const params = new URLSearchParams({ limit: String(query.limit ?? 50) });
  if (query.machineId !== undefined) params.set("machine_id", query.machineId);
  if (query.harness !== undefined) params.set("harness", query.harness);
  if (query.cursor !== undefined) params.set("cursor", query.cursor);
  if (query.parentSessionId !== undefined) params.set("parent_session_id", query.parentSessionId);
  return params.toString();
}

export const httpApi: SessionBrowserApi = {
  async listMachines(signal) {
    const page = await get<{ data: Machine[] }>("/api/machines", signal);
    return page.data;
  },
  async listImportProgress(signal) {
    const page = await get<{ data: MachineImportProgress[] }>("/api/import-progress", signal);
    return page.data;
  },
  listSessions(query, signal) {
    return get<SessionPage<SessionSummary>>(`/api/sessions?${sessionQuery(query)}`, signal);
  },
  getSession(sessionId, signal) {
    return get<SessionSummary>(`/api/sessions/${encodeURIComponent(sessionId)}`, signal);
  },
  async listVersions(sessionId, signal) {
    const page = await get<{ data: SessionVersion[] }>(
      `/api/sessions/${encodeURIComponent(sessionId)}/versions`,
      signal
    );
    return page.data;
  },
  listEvents(sessionId, versionId, cursor, signal) {
    const params = new URLSearchParams({ limit: "100" });
    if (cursor !== undefined) params.set("cursor", String(cursor));
    const path = `/api/sessions/${encodeURIComponent(sessionId)}/versions/${encodeURIComponent(versionId)}/events?${params}`;
    return get<EventPage<EventRecord>>(path, signal);
  },
};

export function harnessLabel(harness: Harness): string {
  return harness === "claude" ? "Claude Code" : "Codex CLI";
}
