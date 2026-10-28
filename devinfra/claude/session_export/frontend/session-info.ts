import type { SessionSummary } from "./api/schema";

export type StatusFilter = "all" | "active" | "paused" | "archived";
const ROUTINE_SESSION_ORIGINS = new Set([
  "force_run_trigger",
  "scheduled_trigger",
  "fire_routine",
  "github_webhook_trigger",
]);
export function mergeSessionPage(latest: SessionSummary[], previous: SessionSummary[]): SessionSummary[] {
  const latestIds = new Set(latest.map((session) => session.id));
  return [...latest, ...previous.filter((session) => !latestIds.has(session.id))];
}
export function sessionMatchesFilter(session: SessionSummary, filter: StatusFilter): boolean {
  return filter === "all" || session.status === filter;
}
export function sortSessions(sessions: SessionSummary[]): SessionSummary[] {
  return [...sessions].sort((left, right) => {
    // Match PostgreSQL's DESC ordering (NULLS FIRST), then the API's stable ID tie-breaker.
    const leftTime = left.last_event_at === null ? Number.POSITIVE_INFINITY : Date.parse(left.last_event_at);
    const rightTime = right.last_event_at === null ? Number.POSITIVE_INFINITY : Date.parse(right.last_event_at);
    const byTimestamp = rightTime - leftTime;
    return (Number.isNaN(byTimestamp) ? 0 : byTimestamp) || right.id.localeCompare(left.id);
  });
}
export function sessionSubtitle(session: SessionSummary): string {
  const fields = session as SessionSummary & { git_branch?: unknown; repo_path?: unknown; repository?: unknown };
  const branch = typeof fields.git_branch === "string" ? fields.git_branch : null;
  const repository =
    typeof fields.repository === "string"
      ? fields.repository
      : typeof fields.repo_path === "string"
        ? fields.repo_path
        : null;
  return [repository, branch].filter((value): value is string => value !== null).join(" · ") || session.id;
}
export function sessionRoutineId(session: SessionSummary): string | null {
  const triggerId = (session as SessionSummary & { trigger_id?: unknown }).trigger_id;
  return typeof triggerId === "string" && triggerId.length > 0 ? triggerId : null;
}
export function isRoutineSession(session: SessionSummary): boolean {
  if (sessionRoutineId(session) !== null) return true;
  const origin = (session as SessionSummary & { origin?: unknown }).origin;
  return typeof origin === "string" && ROUTINE_SESSION_ORIGINS.has(origin);
}
export function statusColor(status: string): "green" | "yellow" | "gray" {
  if (status === "active") return "green";
  if (status === "paused") return "yellow";
  return "gray";
}
