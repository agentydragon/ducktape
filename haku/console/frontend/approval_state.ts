import type { PendingApproval, ToolCallRecord } from "./client.ts";
import type { GeolocationOptions } from "./bridge.ts";

export interface GeolocationApproval {
  id: string;
  mode: "geolocation" | "geolocationWatch";
  options?: GeolocationOptions;
  createdAt: string;
}

interface ApprovalQueueItemBase {
  id: string;
  createdAt: string;
}

export type ApprovalQueueItem =
  | (ApprovalQueueItemBase & { kind: "tool"; approval: PendingApproval })
  | (ApprovalQueueItemBase & { kind: "geolocation"; approval: GeolocationApproval });

export interface ApprovalDisplayFields {
  title: string;
  serverId: string;
  toolName: string;
  rationale: string;
  argumentsJson: string;
  argumentSummary: string;
  toolCallId: string;
  callerPrincipal: string | null;
  createdAt: string | null;
  denialReason: string | null;
}

export interface RecentToolCall {
  record: ToolCallRecord;
  hideAtMs: number;
}

export interface RecentToolCallCountdown {
  label: string;
  progressPercent: number;
  remainingSeconds: number;
}

const RECENT_OK_TTL_MS = 15_000;
const RECENT_ERROR_TTL_MS = 60_000;

function createdAtMs(value: string | undefined): number {
  if (!value) return 0;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}

export function toolApprovalQueueId(toolCallId: string): string {
  return `tool:${toolCallId}`;
}

export function geolocationApprovalQueueId(id: string): string {
  return `geolocation:${id}`;
}

export function approvalQueueItems(
  toolApprovals: readonly PendingApproval[],
  geolocationApprovals: readonly GeolocationApproval[]
): ApprovalQueueItem[] {
  return [
    ...toolApprovals.map(
      (approval): ApprovalQueueItem => ({
        kind: "tool",
        id: toolApprovalQueueId(approval.tool_call_id),
        createdAt: approval.created_at,
        approval,
      })
    ),
    ...geolocationApprovals.map(
      (approval): ApprovalQueueItem => ({
        kind: "geolocation",
        id: geolocationApprovalQueueId(approval.id),
        createdAt: approval.createdAt,
        approval,
      })
    ),
  ].sort((a, b) => createdAtMs(b.createdAt) - createdAtMs(a.createdAt));
}

export function approvalDisplayFields(approval: PendingApproval | ToolCallRecord): ApprovalDisplayFields {
  const args = approval.arguments ?? {};
  const toolName = approval.tool_name ?? "unknown tool";
  return {
    title: approval.title ?? `${approval.server_id}: ${toolName}`,
    serverId: approval.server_id,
    toolName,
    rationale: approval.rationale ?? "",
    argumentsJson: JSON.stringify(args, null, 2) ?? "null",
    argumentSummary: summarizeArguments(args),
    toolCallId: approval.tool_call_id,
    callerPrincipal: approval.caller_principal ?? null,
    createdAt: approval.created_at ?? null,
    denialReason: "denial_reason" in approval ? (approval.denial_reason ?? null) : null,
  };
}

export function summarizeArguments(args: Record<string, unknown>): string {
  const keys = Object.keys(args);
  if (keys.length === 0) return "No arguments";
  if (keys.length <= 3) return keys.join(", ");
  return `${keys.length} fields: ${keys.slice(0, 3).join(", ")}`;
}

export function recentToolCallTtlMs(status: ToolCallRecord["status"]): number | null {
  if (status === "ok" || status === "denied") return RECENT_OK_TTL_MS;
  if (status === "error") return RECENT_ERROR_TTL_MS;
  return null;
}

export function makeRecentToolCall(record: ToolCallRecord, nowMs: number): RecentToolCall | null {
  const ttlMs = recentToolCallTtlMs(record.status);
  return ttlMs === null ? null : { record, hideAtMs: nowMs + ttlMs };
}

export function recentToolCallCountdown(recent: RecentToolCall, nowMs: number): RecentToolCallCountdown {
  const ttlMs = recentToolCallTtlMs(recent.record.status) ?? 0;
  const remainingMs = Math.max(0, recent.hideAtMs - nowMs);
  const remainingSeconds = Math.ceil(remainingMs / 1000);
  const progressPercent = ttlMs === 0 ? 0 : Math.max(0, Math.min(100, (remainingMs / ttlMs) * 100));
  return {
    label: remainingSeconds === 0 ? "Auto-hides now" : `Auto-hides in ${remainingSeconds}s`,
    progressPercent,
    remainingSeconds,
  };
}

export function terminalStatusLabel(status: ToolCallRecord["status"]): string {
  if (status === "ok") return "OK";
  if (status === "denied") return "Denied";
  if (status === "error") return "Error";
  if (status === "running") return "Running";
  return "Pending";
}

export function geolocationApprovalTitle(approval: GeolocationApproval): string {
  return approval.mode === "geolocationWatch" ? "Allow continuous location sharing?" : "Allow location sharing?";
}

export function geolocationApprovalBody(approval: GeolocationApproval): string {
  return approval.mode === "geolocationWatch"
    ? "Haku is asking to continuously receive your device location until it stops the watch or you withdraw access."
    : "Haku is asking to read your current device location once.";
}
