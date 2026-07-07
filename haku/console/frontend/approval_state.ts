import type { PendingApproval, ToolCallRecord } from "./client.ts";
import type { GeolocationOptions } from "./bridge.ts";

export interface GeolocationApproval {
  id: string;
  mode: "geolocation" | "geolocationWatch";
  options?: GeolocationOptions;
  createdAt: string;
}

export type ApprovalQueueItem =
  | { kind: "tool"; id: string; createdAt: string; approval: PendingApproval }
  | { kind: "geolocation"; id: string; createdAt: string; approval: GeolocationApproval };

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
}

export interface RecentToolCall {
  record: ToolCallRecord;
  hideAtMs: number;
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

export function sortApprovalsNewestFirst(approvals: readonly PendingApproval[]): PendingApproval[] {
  return [...approvals].sort((a, b) => createdAtMs(b.created_at) - createdAtMs(a.created_at));
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

export function nextSelectedApprovalId(
  toolApprovals: readonly PendingApproval[],
  geolocationApprovals: readonly GeolocationApproval[],
  selectedId: string | null
): string | null {
  const items = approvalQueueItems(toolApprovals, geolocationApprovals);
  if (selectedId && items.some((item) => item.id === selectedId)) return selectedId;
  return items[0]?.id ?? null;
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

export function formatHideCountdown(hideAtMs: number, nowMs: number): string {
  const remainingSeconds = Math.max(0, Math.ceil((hideAtMs - nowMs) / 1000));
  return remainingSeconds === 0 ? "hiding now" : `hiding in ${remainingSeconds}s`;
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
