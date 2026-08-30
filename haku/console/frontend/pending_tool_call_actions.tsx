import { Button, Group, Textarea } from "@mantine/core";
import { useState } from "react";

import { SUCCESS_COLOR } from "./theme";

// The approve/deny control for a pending tool call, shared by the approvals panel card and the
// history row. The optional free-text note sits to the LEFT of the buttons on one row.
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
  onApprove: (decisionNote?: string) => void;
  onDeny: (decisionNote?: string) => void;
}): JSX.Element {
  const [decisionNote, setDecisionNote] = useState("");
  const disabled = busy || !armed;
  const normalizedNote = () => decisionNote.trim() || undefined;
  return (
    <Group gap="xs" align="flex-end" wrap="nowrap">
      <Textarea
        size="xs"
        placeholder="Operator note (optional)"
        aria-label="Operator note"
        autosize
        minRows={1}
        maxRows={4}
        disabled={busy}
        value={decisionNote}
        onChange={(e) => setDecisionNote(e.currentTarget.value)}
        style={{ flex: 1 }}
      />
      <Button
        size="xs"
        variant="light"
        color="red"
        loading={busy}
        disabled={disabled}
        onClick={() => onDeny(normalizedNote())}
      >
        Deny
      </Button>
      <Button
        size="xs"
        color={SUCCESS_COLOR}
        loading={busy}
        disabled={disabled}
        onClick={() => onApprove(normalizedNote())}
      >
        Approve
      </Button>
    </Group>
  );
}
