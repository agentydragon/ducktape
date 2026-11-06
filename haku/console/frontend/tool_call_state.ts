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

// Keep every status accounted for instead of silently rendering it as a blue "Pending" badge.
const STATUS_PRESENTATION = {
  pending_approval: { label: "Pending", color: "orange" },
  running: { label: "Running", color: "blue" },
  ok: { label: "OK", color: "teal" },
  error: { label: "Error", color: "red" },
  denied: { label: "Denied", color: "gray" },
  withdrawn: { label: "Withdrawn", color: "gray" },
} satisfies Record<ToolCallRecord["status"], { label: string; color: string }>;

export function terminalStatusLabel(status: ToolCallRecord["status"]): string {
  return STATUS_PRESENTATION[status]?.label;
}

export function statusColor(status: ToolCallRecord["status"]): string {
  return STATUS_PRESENTATION[status]?.color;
}
