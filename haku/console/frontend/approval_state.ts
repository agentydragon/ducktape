import type { GeolocationOptions } from "@haku/console-bridge/protocol";

import type { ToolCallRecord } from "./client";

export interface GeolocationApproval {
  id: string;
  mode: "geolocation" | "geolocationWatch";
  options?: GeolocationOptions;
  createdAt: string;
}

export interface ScreenshotApproval {
  id: string;
  createdAt: string;
}

export type CapabilityRequestItem =
  | { kind: "geolocation"; id: string; createdAt: string; approval: GeolocationApproval }
  | { kind: "screenshot"; id: string; createdAt: string; approval: ScreenshotApproval };

function createdAtMs(value: string): number {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}

export function geolocationApprovalQueueId(id: string): string {
  return `geolocation:${id}`;
}

export function screenshotApprovalQueueId(id: string): string {
  return `screenshot:${id}`;
}

export function capabilityRequestItems(
  geolocationRequests: readonly GeolocationApproval[],
  screenshotRequests: readonly ScreenshotApproval[]
): CapabilityRequestItem[] {
  return [
    ...geolocationRequests.map((approval): CapabilityRequestItem => ({
      kind: "geolocation",
      id: geolocationApprovalQueueId(approval.id),
      createdAt: approval.createdAt,
      approval,
    })),
    ...screenshotRequests.map((approval): CapabilityRequestItem => ({
      kind: "screenshot",
      id: screenshotApprovalQueueId(approval.id),
      createdAt: approval.createdAt,
      approval,
    })),
  ].sort((a, b) => createdAtMs(b.createdAt) - createdAtMs(a.createdAt));
}

export interface ApprovalDisplayFields {
  title: string;
  serverId: string;
  toolName: string;
  rationale: string;
  argumentsJson: string;
  toolCallId: string;
  callerDisplayName: string;
  createdAt: string | null;
  decisionNote: string | null;
  decisionOperatorId: string | null;
  withdrawalReason: string | null;
  approvalPolicyId: string | null;
  autoApprovalEvaluation: string | null;
}

export function approvalDisplayFields(approval: ToolCallRecord): ApprovalDisplayFields {
  const args = approval.arguments ?? {};
  const toolName = approval.tool_name ?? "unknown tool";
  return {
    title: approval.title ?? `${approval.server_id}: ${toolName}`,
    serverId: approval.server_id,
    toolName,
    rationale: approval.rationale ?? "",
    argumentsJson: JSON.stringify(args, null, 2) ?? "null",
    toolCallId: approval.tool_call_id,
    callerDisplayName: approval.caller.kind === "agent" ? approval.caller.display_name : "Operator",
    createdAt: approval.created_at ?? null,
    decisionNote: approval.decision_note ?? null,
    decisionOperatorId: approval.decision_operator_id ?? null,
    withdrawalReason: approval.withdrawal_reason ?? null,
    approvalPolicyId: approval.approval_policy_id ?? null,
    autoApprovalEvaluation: approval.auto_approval_evaluation ?? null,
  };
}

// Keyed maps rather than if-chains with a fallback: a new status is then a type error here instead
// of silently rendering as a blue "Pending" badge.
const STATUS_LABELS: Record<ToolCallRecord["status"], string> = {
  pending_approval: "Pending",
  running: "Running",
  ok: "OK",
  error: "Error",
  denied: "Denied",
  withdrawn: "Withdrawn",
};

const STATUS_COLORS: Record<ToolCallRecord["status"], string> = {
  // Approval is an operator decision, so keep it distinct from a running call and bright enough
  // to read against the dark shell background.
  pending_approval: "orange",
  running: "blue",
  ok: "teal",
  error: "red",
  denied: "gray",
  withdrawn: "gray",
};

/** Whether the card should show the auto-approval evaluation string.
 *
 * A policy that matched says what let the call through unattended, which earns a compact line. One
 * that did not only explains an absence the operator is already looking at, so it is provenance and
 * compact omits it. Detailed carries it either way. */
export function showsAutoApprovalEvaluation(
  fields: Pick<ApprovalDisplayFields, "autoApprovalEvaluation" | "approvalPolicyId">,
  detailed: boolean
): boolean {
  if (!fields.autoApprovalEvaluation) return false;
  return detailed || fields.approvalPolicyId !== null;
}

export function terminalStatusLabel(status: ToolCallRecord["status"]): string {
  return STATUS_LABELS[status];
}

export function statusColor(status: ToolCallRecord["status"]): string {
  return STATUS_COLORS[status];
}

export function geolocationApprovalTitle(approval: GeolocationApproval): string {
  return approval.mode === "geolocationWatch" ? "Allow continuous location sharing?" : "Allow location sharing?";
}

export function geolocationApprovalBody(approval: GeolocationApproval): string {
  return approval.mode === "geolocationWatch"
    ? "Haku is asking to continuously receive your device location until it stops the watch or you withdraw access."
    : "Haku is asking to read your current device location once.";
}

export const SCREENSHOT_APPROVAL_TITLE = "Allow screen capture for a screenshot?";
export const SCREENSHOT_APPROVAL_BODY: string =
  "Haku is asking to capture a screenshot of this page. Approving opens your browser's own " +
  "share-this-tab picker; once granted, further screenshots are instant until you withdraw or " +
  "stop sharing.";
