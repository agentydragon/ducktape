import { formatTimestamp } from "./approval_state.ts";
import { Field } from "./field.tsx";

/** The provenance a tool call carries that isn't about *what it does* — who asked, the exact
 * request time, and the canonical id. Rarely needed while triaging, so it rides behind one
 * collapsed "Metadata" disclosure in the detailed drawer cards and history rows, keeping the
 * detailed body focused on the arguments/result. */
export function ToolCallMeta({
  callerPrincipal,
  createdAt,
  toolCallId,
}: {
  callerPrincipal: string | null;
  createdAt: string | null;
  toolCallId: string;
}) {
  const requested = createdAt ? formatTimestamp(createdAt) : null;
  return (
    <details className="haku-shell-disclosure">
      <summary>Metadata</summary>
      <div className="haku-shell-fields haku-shell-disclosure-body">
        {callerPrincipal && <Field label="Caller">{callerPrincipal}</Field>}
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
