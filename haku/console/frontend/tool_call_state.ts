import type { ToolCallRecord } from "./tool_call_record";

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

// Keyed maps make a new status a type error instead of silently rendering as a blue "Pending" badge.
const STATUS_LABELS: Record<ToolCallRecord["status"], string> = {
  pending_approval: "Pending",
  running: "Running",
  ok: "OK",
  error: "Error",
  denied: "Denied",
  withdrawn: "Withdrawn",
};

const STATUS_COLORS: Record<ToolCallRecord["status"], string> = {
  pending_approval: "orange",
  running: "blue",
  ok: "teal",
  error: "red",
  denied: "gray",
  withdrawn: "gray",
};

export function terminalStatusLabel(status: ToolCallRecord["status"]): string {
  return STATUS_LABELS[status];
}

export function statusColor(status: ToolCallRecord["status"]): string {
  return STATUS_COLORS[status];
}
