import { Badge, Group, Stack, Text } from "@mantine/core";
import type { ReactNode } from "react";

import type { ApprovalDisplayFields } from "./approval_state.ts";
import { Field } from "./field.tsx";
import { ToolActionLine } from "./tool_action_line.tsx";
import { ToolArgumentsField } from "./tool_arguments_field.tsx";
import { ToolCallMeta } from "./tool_call_meta.tsx";
import type { PreviewVariant } from "./tool_previews/variant.tsx";
import { VariantToggle } from "./variant_toggle.tsx";

/** One tool call, rendered the same way everywhere it appears — the drawer's pending and recent
 * cards and the history page's rows. It owns the shared skeleton (the identity header + action
 * line + rationale/error/denial subhead + status badge + Details toggle, the arguments body, and
 * the detailed Result/Metadata) so all of that reads one way and lives in one place. The bits
 * that genuinely differ per surface — the status badge's label/color and the footer actions
 * (approve/deny, dismiss, countdown) — come in as props. */
export function ToolCallCard({
  fields,
  args,
  variant,
  onToggle,
  status,
  error = null,
  result,
  footer,
}: {
  fields: ApprovalDisplayFields;
  args: Record<string, unknown>;
  variant: PreviewVariant;
  onToggle: () => void;
  status: { label: string; color: string };
  error?: string | null;
  result?: unknown;
  footer?: ReactNode;
}) {
  const detailed = variant === "detailed";
  return (
    <section className="haku-shell-card">
      <Stack gap="sm">
        <Group justify="space-between" align="flex-start" gap="sm" wrap="nowrap">
          <Stack gap={2} style={{ minWidth: 0 }}>
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
          </Stack>
          <Badge color={status.color} variant="light" style={{ flexShrink: 0 }}>
            {status.label}
          </Badge>
        </Group>
        <ToolArgumentsField
          serverId={fields.serverId}
          toolName={fields.toolName}
          args={args}
          argumentsJson={fields.argumentsJson}
          variant={variant}
        />
        {detailed && (
          <>
            {result != null && (
              <div className="haku-shell-fields">
                <Field label="Result">
                  <pre className="haku-shell-json">{JSON.stringify(result, null, 2)}</pre>
                </Field>
              </div>
            )}
            <ToolCallMeta
              serverId={fields.serverId}
              toolName={fields.toolName}
              callerPrincipal={fields.callerPrincipal}
              createdAt={fields.createdAt}
              toolCallId={fields.toolCallId}
            />
          </>
        )}
        <VariantToggle variant={variant} onToggle={onToggle} />
        {footer}
      </Stack>
    </section>
  );
}
