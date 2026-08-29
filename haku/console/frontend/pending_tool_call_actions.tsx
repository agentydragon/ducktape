import { Button, Group, Textarea } from "@mantine/core";
import { useState } from "react";

import { SUCCESS_COLOR } from "./theme";

// The approve/deny control for a pending tool call, shared by the approvals panel card and the
// history row. The optional free-text reason sits to the LEFT of the buttons on one row, its
// purpose carried by the placeholder rather than a stacked label.
//
// TODO(remarks-on-approve): let the same note ride an *approve* too, not only a deny — a general
// operator remark the agent can read back from the tool-call result DB. Needs the decision
// endpoint to persist a reason on approve (mcp/approval.py) and a neutral placeholder here.
export function PendingToolCallActions({
  busy,
  // The approvals panel arms its buttons after a short delay, guarding against a misclick on a card
  // that just appeared; the history page has no such delay, so this defaults to armed.
  armed = true,
  onApprove,
  onDeny,
}: {
  busy: boolean;
  armed?: boolean;
  onApprove: () => void;
  onDeny: (reason?: string) => void;
}): JSX.Element {
  const [reason, setReason] = useState("");
  const disabled = busy || !armed;
  return (
    <Group gap="xs" align="flex-end" wrap="nowrap">
      <Textarea
        size="xs"
        placeholder="Denial reason (optional)"
        aria-label="Denial reason"
        autosize
        minRows={1}
        maxRows={4}
        disabled={busy}
        value={reason}
        onChange={(e) => setReason(e.currentTarget.value)}
        style={{ flex: 1 }}
      />
      <Button
        size="xs"
        variant="light"
        color="red"
        loading={busy}
        disabled={disabled}
        onClick={() => onDeny(reason.trim() || undefined)}
      >
        Deny
      </Button>
      <Button size="xs" color={SUCCESS_COLOR} loading={busy} disabled={disabled} onClick={onApprove}>
        Approve
      </Button>
    </Group>
  );
}
