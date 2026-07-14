import { formatTimestamp } from "./approval_state.ts";
import { Field } from "./field.tsx";

/** The provenance a tool call carries that isn't about *what it does* — who asked, the exact
 * request time, and the canonical id. Rarely needed while triaging, so it rides behind one
 * collapsed "Metadata" disclosure in the detailed approvals panel cards and history rows, keeping the
 * detailed body focused on the arguments/result. */
export function ToolCallMeta({
  serverId,
  toolName,
  callerDisplayName,
  createdAt,
  toolCallId,
}: {
  serverId: string;
  toolName: string;
  callerDisplayName: string;
  createdAt: string | null;
  toolCallId: string;
}) {
  const requested = createdAt ? formatTimestamp(createdAt) : null;
  return (
    <details className="haku-shell-disclosure">
      <summary>Metadata</summary>
      <div className="haku-shell-fields haku-shell-disclosure-body">
        <Field label="Tool" mono>
          {serverId}.{toolName}
        </Field>
        <Field label="Caller">{callerDisplayName}</Field>
        {requested && (
          <Field label="Requested">
            <span title={requested.title}>{requested.text}</span>
          </Field>
        )}
        <Field label="Tool call id" mono>
          {toolCallId}
        </Field>
      </div>
    </details>
  );
}
