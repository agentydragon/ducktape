import { Badge, Group, Stack, Text } from "@mantine/core";
import type { ReactNode } from "react";

import type { ApprovalDisplayFields } from "./approval_state.ts";
import { ToolActionLine } from "./tool_action_line.tsx";
import { ToolArgumentsField } from "./tool_arguments_field.tsx";
import { ToolCallMeta } from "./tool_call_meta.tsx";
import type { PreviewVariant } from "./tool_rendering/vocabulary.tsx";
import { ToolResultField } from "./tool_result_field.tsx";
import { VariantControl } from "./variant_control.tsx";

/** One tool call, rendered the same way everywhere it appears — the drawer's pending and recent
 * cards and the history page's rows. It owns the shared skeleton (the identity header + action
 * line + rationale/error/denial subhead + status badge + Details toggle, the arguments body,
 * the result body, and the detailed Metadata) so all of that reads one way and lives in one
 * place. The bits that genuinely differ per surface — the status badge's label/color and the
 * footer actions (approve/deny, dismiss, countdown) — come in as props. */
export function ToolCallCard({
  fields,
  args,
  variant,
  onVariantChange,
  status,
  error = null,
  result,
  footer,
}: {
  fields: ApprovalDisplayFields;
  args: Record<string, unknown>;
  variant: PreviewVariant;
  onVariantChange: (v: PreviewVariant) => void;
  status: { label: string; color: string };
  error?: string | null;
  result?: unknown;
  footer?: ReactNode;
}) {
  const detailed = variant === "detailed";
  return (
    <section className="haku-shell-card">
      <Stack gap="sm">
        {/* The badge + Brief/Full selector float to the top-right so the title and subheads wrap
            under them on the first line(s) and reclaim the full width below, instead of the whole
            text column being narrowed for every line. Anchored top so detail expands below and the
            selector stays put under the pointer. Block flow (not a flex Stack) so text wraps
            around the float; `haku-card-head` supplies the tight inter-line rhythm. */}
        <div className="haku-card-head">
          <Group className="haku-card-head-actions" gap="xs" align="center" wrap="nowrap">
            <Badge color={status.color} variant="light">
              {status.label}
            </Badge>
            <VariantControl variant={variant} onChange={onVariantChange} />
          </Group>
          <Text fw={600} size="sm">
            {fields.title}
          </Text>
          <ToolActionLine serverId={fields.serverId} toolName={fields.toolName} args={args} />
          {fields.rationale && <Text size="xs">{fields.rationale}</Text>}
          {error && (
            <Text size="xs" c="red">
              {error}
            </Text>
          )}
          {fields.denialReason && (
            <Text size="xs" c="dimmed">
              Denied: {fields.denialReason}
            </Text>
          )}
          {fields.approvalPolicyId && (
            <Text size="xs" c="dimmed">
              Auto-approved by {fields.approvalPolicyId}
            </Text>
          )}
          {fields.autoApprovalEvaluation && (
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
        {/* Rendered for both variants; it self-gates (compact shows a result only when a
            per-tool widget makes it self-describing). */}
        <ToolResultField serverId={fields.serverId} toolName={fields.toolName} result={result} variant={variant} />
        {detailed && (
          <ToolCallMeta
            serverId={fields.serverId}
            toolName={fields.toolName}
            callerPrincipal={fields.callerPrincipal}
            createdAt={fields.createdAt}
            toolCallId={fields.toolCallId}
          />
        )}
        {footer}
      </Stack>
    </section>
  );
}
