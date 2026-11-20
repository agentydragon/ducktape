import { Group, Loader, Stack, Text } from "@mantine/core";
import type { JSX, ReactNode, Ref } from "react";

import {
  showsAutoApprovalEvaluation,
  statusColor,
  terminalStatusLabel,
  type ApprovalDisplayFields,
} from "./tool_call_state";
import type { ToolCallRecord } from "./tool_call_record";
import { ClockIcon, CloseIcon, SyncCurrentIcon, SyncErrorIcon } from "./icons";
import { ToolArgumentsField } from "./tool_arguments_field";
import { ToolCallMeta } from "./tool_call_meta";
import { isDestructiveToolCall } from "./tool_rendering/actions";
import type { PreviewVariant } from "./tool_rendering/vocabulary";
import { ToolResultField } from "./tool_result_field";
import { VariantControl } from "./variant_control";

/** One tool call, rendered the same way everywhere it appears — the approvals panel's pending and
 * recent cards, and the history page's rows. It owns the shared skeleton: identity header, action
 * line, rationale/denial subhead, status marker, Details toggle, the arguments body, the result body
 * (a finished call's result, or its error when it failed) and the detailed Metadata. What differs
 * per surface — the status marker's label/color and the footer actions (approve/deny, dismiss,
 * countdown) — comes in as props. */
function ToolCallStatus({ status }: { status: ToolCallRecord["status"] }): JSX.Element {
  const label = terminalStatusLabel(status);
  const color = statusColor(status);
  const icon =
    status === "running" ? (
      <Loader size={14} color={color} />
    ) : status === "error" ? (
      <SyncErrorIcon size={16} color={color} />
    ) : status === "ok" ? (
      <SyncCurrentIcon size={16} color={color} />
    ) : status === "denied" || status === "withdrawn" ? (
      <CloseIcon size={16} color={color} />
    ) : (
      <ClockIcon size={16} color={color} />
    );
  return (
    <span className="haku-tool-call-status" role="img" aria-label={`Status: ${label}`} title={label}>
      {icon}
    </span>
  );
}

export function ToolCallCard({
  fields,
  args,
  variant,
  onVariantChange,
  status,
  error = null,
  result,
  footer,
  containerRef,
}: {
  fields: ApprovalDisplayFields;
  args: Record<string, unknown>;
  variant: PreviewVariant;
  onVariantChange: (v: PreviewVariant) => void;
  status: ToolCallRecord["status"];
  error?: string | null;
  result?: unknown;
  footer?: ReactNode;
  /** Set by a surface that needs to scroll this specific card into view (a deep-linked call). */
  containerRef?: Ref<HTMLElement>;
}): JSX.Element {
  const detailed = variant === "detailed";
  const destructive = isDestructiveToolCall(fields.serverId, fields.toolName);
  return (
    <section className="haku-tool-call" ref={containerRef}>
      <Stack gap="sm">
        <div className="haku-tool-call-summary">
          <Text
            fw={600}
            size="sm"
            className="haku-tool-call-title"
            c={destructive || status === "error" ? "red" : status === "running" ? "blue" : undefined}
          >
            {fields.title}
          </Text>
          <Group className="haku-tool-call-summary-actions" gap="xs" align="center" wrap="nowrap">
            <ToolCallStatus status={status} />
            <VariantControl variant={variant} onChange={onVariantChange} />
          </Group>
        </div>
        <div className="haku-card-head">
          {fields.rationale && <Text size="xs">{fields.rationale}</Text>}
          {fields.decisionNote && (
            <Text size="xs" c="dimmed">
              {fields.decisionOperatorId ? "Operator note" : "Automatic decision"}: {fields.decisionNote}
            </Text>
          )}
          {fields.withdrawalReason && (
            <Text size="xs" c="dimmed">
              Withdrawn: {fields.withdrawalReason}
            </Text>
          )}
          {fields.approvalPolicyId && (
            <Text size="xs" c="dimmed">
              Auto-approved by {fields.approvalPolicyId}
            </Text>
          )}
          {showsAutoApprovalEvaluation(fields, detailed) && (
            <Text size="xs" c="dimmed">
              Auto-approval: {fields.autoApprovalEvaluation}
            </Text>
          )}
        </div>
        <ToolArgumentsField
          serverId={fields.serverId}
          toolName={fields.toolName}
          args={args}
          argumentsJson={fields.argumentsJson}
          variant={variant}
        />
        {/* A failed call's error is its outcome — the failure counterpart of the result body
            below — so it renders under the arguments, not as a head subhead. Error and result
            are mutually exclusive (the ledger finishes a call with exactly one), so this never
            collides with the result field. */}
        {error && (
          <Text size="sm" c="red">
            {error}
          </Text>
        )}
        <ToolResultField result={result} variant={variant} />
        {detailed && (
          <ToolCallMeta
            serverId={fields.serverId}
            toolName={fields.toolName}
            callerDisplayName={fields.callerDisplayName}
            createdAt={fields.createdAt}
            toolCallId={fields.toolCallId}
          />
        )}
        {footer}
      </Stack>
    </section>
  );
}
