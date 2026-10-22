import type { JSX } from "react";
import { Field } from "./field";
import { formatTimestamp } from "./time";
import { RelativeTime } from "../../../util/frontend/relative_time";

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
}): JSX.Element {
  return (
    <details className="haku-shell-disclosure">
      <summary>Metadata</summary>
      <div className="haku-shell-fields haku-shell-disclosure-body">
        <Field label="Tool" mono>
          {serverId}.{toolName}
        </Field>
        <Field label="Caller">{callerDisplayName}</Field>
        {createdAt && (
          <Field label="Requested">
            <RelativeTime value={new Date(createdAt)} format={formatTimestamp} />
          </Field>
        )}
        <Field label="Tool call id" mono>
          {toolCallId}
        </Field>
      </div>
    </details>
  );
}
