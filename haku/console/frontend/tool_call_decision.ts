import { useCallback, useState } from "react";

import { approvalDisplayFields, terminalStatusLabel } from "./approval_state";
import { approveToolCall, denyToolCall, type ToolCallRecord } from "./client";
import { toastError, toastSuccess } from "./toast";

type ToolCallDecision = "approve" | "deny";
export interface ToolCallDecisionDependencies {
  approve: typeof approveToolCall;
  deny: typeof denyToolCall;
  success: typeof toastSuccess;
  error: typeof toastError;
}

const DEFAULT_DEPENDENCIES: ToolCallDecisionDependencies = {
  approve: approveToolCall,
  deny: denyToolCall,
  success: toastSuccess,
  error: toastError,
};

export interface ToolCallDecisionOptions {
  dependencies?: ToolCallDecisionDependencies;
  onSuccess?: (record: ToolCallRecord) => void;
  onSettled?: () => void;
}

export function toolCallDecisionFeedback(
  decision: ToolCallDecision,
  record: ToolCallRecord
): { title: string; message: string } {
  // Approving does not run the tool inline: the backend dispatches execution and returns the
  // RUNNING record, so the result arrives later via the live WS refetch, not in this response.
  return {
    title: decision === "approve" ? "Tool call approved" : "Tool call denied",
    message: `${approvalDisplayFields(record).title}: ${terminalStatusLabel(record.status)}`,
  };
}

export async function executeToolCallDecision(
  call: ToolCallRecord,
  decision: ToolCallDecision,
  decisionNote?: string,
  dependencies: ToolCallDecisionDependencies = DEFAULT_DEPENDENCIES
): Promise<ToolCallRecord | null> {
  let record: ToolCallRecord;
  try {
    record =
      decision === "approve"
        ? await dependencies.approve(call.tool_call_id, decisionNote)
        : await dependencies.deny(call.tool_call_id, decisionNote);
  } catch (error) {
    dependencies.error("Tool call decision failed", error);
    return null;
  }
  const feedback = toolCallDecisionFeedback(decision, record);
  dependencies.success(feedback.title, feedback.message);
  return record;
}

export function useToolCallDecision({ dependencies, onSuccess, onSettled }: ToolCallDecisionOptions = {}): {
  decidingToolCallIds: ReadonlySet<string>;
  isDeciding: (toolCallId: string) => boolean;
  approve: (call: ToolCallRecord, decisionNote?: string) => Promise<void>;
  deny: (call: ToolCallRecord, decisionNote?: string) => Promise<void>;
} {
  const [decidingToolCallIds, setDecidingToolCallIds] = useState<ReadonlySet<string>>(() => new Set());

  const decide = useCallback(
    async (call: ToolCallRecord, decision: ToolCallDecision, decisionNote?: string) => {
      setDecidingToolCallIds((ids) => new Set(ids).add(call.tool_call_id));
      try {
        const record = await executeToolCallDecision(call, decision, decisionNote, dependencies);
        if (record) onSuccess?.(record);
      } finally {
        setDecidingToolCallIds((ids) => {
          const remaining = new Set(ids);
          remaining.delete(call.tool_call_id);
          return remaining;
        });
        onSettled?.();
      }
    },
    [dependencies, onSettled, onSuccess]
  );

  return {
    decidingToolCallIds,
    isDeciding: (toolCallId: string) => decidingToolCallIds.has(toolCallId),
    approve: (call: ToolCallRecord, decisionNote?: string) => decide(call, "approve", decisionNote),
    deny: (call: ToolCallRecord, decisionNote?: string) => decide(call, "deny", decisionNote),
  };
}
